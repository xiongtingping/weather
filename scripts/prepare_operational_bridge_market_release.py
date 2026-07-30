#!/usr/bin/env python3
"""Prepare content-addressed bridge/context files without touching active pointers."""

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
    build_operational_bridge_release,
    latest_target_contract_for_date,
)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(*, event_date: str, approved_at: str, registry: Path, apply: bool) -> dict:
    source = json.loads(
        (ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text(encoding="utf-8")
    )
    bridge = build_operational_bridge_release(
        source_authority_contract=source, approved_at=approved_at
    )
    target = latest_target_contract_for_date(registry, event_date=event_date)
    context = build_daily_market_context_release(
        target_contract=target, bridge_artifact=bridge["production_artifact"]
    )
    receipt_path = (
        ROOT / "config/artifacts/resolution_bridge/approvals"
        / f"{bridge['approval_receipt']['approval_receipt_sha256']}.json"
    )
    paths = {
        "bridge_artifact": ROOT / bridge["production_artifact_path"],
        "bridge_approval_receipt": receipt_path,
        "market_context": ROOT / context["context_path"],
        "candidate_bridge_activation": ROOT / "data/candidates/resolution_bridge_production_activation.json",
        "candidate_market_context_activation": ROOT / "data/candidates/production_probability_v6_market_context_activation.json",
    }
    if apply:
        _write(paths["bridge_artifact"], bridge["production_artifact"])
        _write(paths["bridge_approval_receipt"], bridge["approval_receipt"])
        _write(paths["market_context"], context["context"])
        _write(paths["candidate_bridge_activation"], bridge["production_activation"])
        _write(paths["candidate_market_context_activation"], context["activation"])
    return {
        "schema": "operational_bridge_market_release_preparation_v1",
        "ok": True,
        "state": "candidate_files_materialized" if apply else "dry_run_ready",
        "event_date": event_date,
        "bridge_artifact_sha256": bridge["production_artifact"]["artifact_sha256"],
        "bridge_activation_sha256": bridge["production_activation"]["activation_sha256"],
        "market_context_sha256": context["context"]["market_context_sha256"],
        "market_context_activation_sha256": context["activation"]["activation_sha256"],
        "paths": {name: str(path) for name, path in paths.items()},
        "sample_or_oos_live_gate": False,
        "hard_exclusion_certificates_authorized": False,
        "external_disk_required": False,
        "writes_archive_db": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--approved-at", default="2026-07-16T10:24:22Z")
    parser.add_argument("--registry", type=Path, default=ROOT / "data/shadow/resolution_final_labels.db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = prepare(
        event_date=args.event_date,
        approved_at=args.approved_at,
        registry=args.registry,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
