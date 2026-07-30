#!/usr/bin/env python3
"""Expose a rehearsed bridge/context pair with a physical-only safe midpoint."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_v6_market_inputs_runtime import prewarm_v6_market_inputs  # noqa: E402
from scripts.production_probability_v6_readiness import audit as probability_audit  # noqa: E402
from scripts.production_reported_x_market_no_order_drill import run_drill  # noqa: E402
from scripts.production_reported_x_market_readiness import audit as market_audit  # noqa: E402


CONFIRM = "ACTIVATE_SAMPLE_FREE_OPERATIONAL_X_S_B_SOFT_PROJECTION"


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def activate(
    *, db: Path, event_date: str, candidate_bridge: Path,
    candidate_context: Path, candidate_receipt: Path, apply: bool, confirm: str,
) -> dict:
    probability = probability_audit(ROOT, db)
    candidate_ready = market_audit(
        db=db,
        probability_activation=ROOT / "config/production_probability_runtime_activation.json",
        bridge_activation=candidate_bridge,
        market_context_activation=candidate_context,
        pair_registry=ROOT / "data/shadow/resolution_final_labels.db",
        no_order_receipt=candidate_receipt,
        event_date=event_date,
    )
    blockers = []
    if probability.get("ok") is not True:
        blockers.append("production_probability_v6_not_ready")
    if candidate_ready.get("production_market_projection_ready") is not True:
        blockers.extend(candidate_ready.get("safety_blockers") or [])
        blockers.extend(candidate_ready.get("promotion_blockers") or [])
    if apply and confirm != CONFIRM:
        blockers.append("exact_activation_confirmation_required")
    if blockers:
        return {
            "schema": "operational_bridge_market_activation_v1", "ok": False,
            "state": "blocked", "blockers": sorted(set(str(item) for item in blockers)),
            "candidate_readiness": candidate_ready,
            "external_disk_required": False, "places_orders": False,
        }
    if not apply:
        return {
            "schema": "operational_bridge_market_activation_v1", "ok": True,
            "state": "ready_to_activate", "candidate_readiness": candidate_ready,
            "linearization_point": "market_context_activation_replace",
            "external_disk_required": False, "places_orders": False,
        }
    active_bridge = ROOT / "config/resolution_bridge_production_activation.json"
    active_context = ROOT / "config/trading/production_probability_v6_market_context_activation.json"
    previous_bridge = active_bridge.read_bytes()
    previous_context = active_context.read_bytes()
    try:
        # Phase 1 is intentionally non-exposing: a new bridge with the old
        # disabled/stale context can only select physical-only Transaction B.
        _write_bytes(active_bridge, candidate_bridge.read_bytes())
        # Phase 2 is the single market exposure linearization point.
        _write_bytes(active_context, candidate_context.read_bytes())
        inputs = prewarm_v6_market_inputs(
            event_date=event_date,
            market_context_activation_path=active_context,
            bridge_activation_path=active_bridge,
            root=ROOT,
        )
        if inputs.get("market_projection_available") is not True:
            raise RuntimeError("active_market_inputs_not_ready")
        receipt_path = ROOT / "data/logs/production_probability_v6_market_no_order_drill.json"
        drill = run_drill(
            db,
            bridge_activation=active_bridge,
            market_context_activation=active_context,
            event_date=event_date,
        )
        drill["receipt_sha256"] = __import__("hashlib").sha256(
            json.dumps(drill, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        _write_bytes(
            receipt_path,
            (json.dumps(drill, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        active_ready = market_audit(
            db=db,
            probability_activation=ROOT / "config/production_probability_runtime_activation.json",
            bridge_activation=active_bridge,
            market_context_activation=active_context,
            pair_registry=ROOT / "data/shadow/resolution_final_labels.db",
            no_order_receipt=receipt_path,
            event_date=event_date,
        )
        if active_ready.get("production_market_projection_ready") is not True:
            raise RuntimeError("active_market_readiness_not_ready")
    except Exception:
        _write_bytes(active_context, previous_context)
        _write_bytes(active_bridge, previous_bridge)
        raise
    return {
        "schema": "operational_bridge_market_activation_v1", "ok": True,
        "state": "production_soft_market_projection_active",
        "candidate_readiness": candidate_ready,
        "active_readiness": active_ready,
        "no_order_drill": drill,
        "linearization_point": "market_context_activation_replace",
        "sample_or_oos_live_gate": False,
        "hard_exclusion_certificates_authorized": False,
        "external_disk_required": False,
        "writes_archive_db": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--candidate-bridge", type=Path, default=ROOT / "data/candidates/resolution_bridge_production_activation.json")
    parser.add_argument("--candidate-context", type=Path, default=ROOT / "data/candidates/production_probability_v6_market_context_activation.json")
    parser.add_argument("--candidate-receipt", type=Path, default=ROOT / "data/candidates/production_probability_v6_market_no_order_drill.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        result = activate(
            db=args.db, event_date=args.event_date,
            candidate_bridge=args.candidate_bridge,
            candidate_context=args.candidate_context,
            candidate_receipt=args.candidate_receipt,
            apply=args.apply, confirm=args.confirm,
        )
    except Exception as exc:
        result = {
            "schema": "operational_bridge_market_activation_v1", "ok": False,
            "state": "rolled_back", "blockers": [f"{type(exc).__name__}:{exc}"],
            "external_disk_required": False, "places_orders": False,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
