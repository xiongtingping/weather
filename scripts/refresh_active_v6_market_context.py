#!/usr/bin/env python3
"""Atomically refresh today's optional market context from the frozen target."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from operational_bridge_release_runtime import (  # noqa: E402
    build_daily_market_context_release,
    latest_target_contract_for_date,
)
from resolution_bridge_activation_runtime import (  # noqa: E402
    load_active_resolution_bridge_artifact,
)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def refresh(
    *, event_date: str, registry: Path, bridge_activation: Path,
    context_activation: Path, apply: bool,
) -> dict:
    bridge, artifact = load_active_resolution_bridge_artifact(
        activation_path=bridge_activation, root=ROOT
    )
    if artifact is None:
        return {
            "schema": "active_v6_market_context_refresh_v1", "ok": True,
            "state": "deferred_bridge_disabled", "event_date": event_date,
            "blocks_physical_x": False, "writes_archive_db": False,
            "places_orders": False,
        }
    target = latest_target_contract_for_date(registry, event_date=event_date)
    release = build_daily_market_context_release(
        target_contract=target, bridge_artifact=artifact
    )
    context_path = ROOT / release["context_path"]
    current = {}
    if context_activation.is_file():
        try:
            current = json.loads(context_activation.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
    already_active = bool(
        current.get("activation_sha256")
        == release["activation"].get("activation_sha256")
    )
    if apply and not already_active:
        # Context artifact first, one active-pointer replace last. Until that
        # final replace the old context remains authoritative.
        _write(context_path, release["context"])
        _write(context_activation, release["activation"])
    return {
        "schema": "active_v6_market_context_refresh_v1",
        "ok": True,
        "state": "already_active" if already_active else "activated" if apply else "ready",
        "event_date": event_date,
        "bridge_activation_sha256": bridge.get("activation_sha256"),
        "bridge_artifact_sha256": artifact.get("artifact_sha256"),
        "market_context_sha256": release["context"].get("market_context_sha256"),
        "market_context_activation_sha256": release["activation"].get("activation_sha256"),
        "context_path": str(context_path),
        "active_pointer": str(context_activation),
        "linearization_point": "atomic_market_context_activation_replace",
        "blocks_physical_x": False,
        "external_disk_required": False,
        "writes_archive_db": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--registry", type=Path, default=ROOT / "data/shadow/resolution_final_labels.db")
    parser.add_argument("--bridge-activation", type=Path, default=ROOT / "config/resolution_bridge_production_activation.json")
    parser.add_argument("--context-activation", type=Path, default=ROOT / "config/trading/production_probability_v6_market_context_activation.json")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = refresh(
        event_date=args.event_date, registry=args.registry,
        bridge_activation=args.bridge_activation,
        context_activation=args.context_activation, apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
