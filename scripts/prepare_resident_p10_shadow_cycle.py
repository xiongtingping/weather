#!/usr/bin/env python3
"""Freeze and append one evidence-supplied P10 prepared cycle.

The input is deliberately limited to a fully materialized P8/P9 cycle plus an
externally observed preparation time and preparer identity.  This command does
not fetch forecasts, create a P0 checkpoint, train/promote an LR artifact,
fill a resolution bridge, sign, or submit an order.  Preview is the default;
append requires both the isolated P10 DB and an explicit confirmation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from p10_shadow_runtime_schema import build_p10_shadow_runtime_schema_plan  # noqa: E402
from resident_p10_shadow_prepared_cycle import (  # noqa: E402
    freeze_resident_p10_shadow_cycle,
    persist_resident_p10_shadow_cycle,
)


INPUT_SCHEMA = "resident_p10_shadow_cycle_prepare_input_v1"
CONFIRMATION = "PERSIST_RESIDENT_P10_PREPARED_CYCLE"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _blocked(*blockers: str) -> dict[str, Any]:
    return {
        "schema": INPUT_SCHEMA,
        "ok": False,
        "state": "blocked",
        "blockers": sorted(set(value for value in blockers if value)),
        "shadow_only": True,
        "places_orders": False,
    }


def _load_input(path: Path) -> tuple[dict[str, Any], list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}, ["resident_p10_preparer_input_unreadable"]
    payload = _mapping(value)
    if payload.get("schema") != INPUT_SCHEMA:
        return {}, ["resident_p10_preparer_input_schema_invalid"]
    if not _mapping(payload.get("cycle_input")):
        return {}, ["resident_p10_preparer_cycle_input_missing"]
    if not str(payload.get("prepared_at") or "").strip():
        return {}, ["resident_p10_preparer_prepared_at_missing"]
    if not str(payload.get("prepared_by") or "").strip():
        return {}, ["resident_p10_preparer_prepared_by_missing"]
    return payload, []


def _preview(payload: Mapping[str, Any]) -> dict[str, Any]:
    frozen = freeze_resident_p10_shadow_cycle(
        _mapping(payload.get("cycle_input")),
        prepared_at=str(payload.get("prepared_at") or ""),
        prepared_by=str(payload.get("prepared_by") or ""),
    )
    if frozen.get("ok") is not True:
        return {
            "schema": INPUT_SCHEMA,
            "ok": False,
            "state": "blocked",
            "blockers": list(frozen.get("blockers") or []),
            "validation": frozen.get("validation"),
            "shadow_only": True,
            "places_orders": False,
        }
    cycle = _mapping(frozen.get("prepared_cycle"))
    return {
        "schema": INPUT_SCHEMA,
        "ok": True,
        "state": "preview_ready",
        "prepared_cycle_id": cycle.get("prepared_cycle_id"),
        "prepared_cycle_sha256": cycle.get("prepared_cycle_sha256"),
        "target_identity": cycle.get("target_identity"),
        "event_date": cycle.get("event_date"),
        "runtime_dependency_identity_sha256": cycle.get("runtime_dependency_identity_sha256"),
        "prepared_cycle": cycle,
        "validation": frozen.get("validation"),
        "shadow_only": True,
        "read_only": True,
        "places_orders": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--shadow-db", default=str(ROOT / "data" / "shadow" / "p10_runtime.db"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)

    input_path = Path(args.input_json).expanduser().resolve()
    shadow_db = Path(args.shadow_db).expanduser().resolve()
    if args.apply and args.confirm != CONFIRMATION:
        result = _blocked("resident_p10_preparer_confirmation_invalid")
    elif shadow_db.name == "archive.db":
        result = _blocked("production_archive_db_forbidden")
    else:
        payload, blockers = _load_input(input_path)
        preview = _preview(payload) if not blockers else _blocked(*blockers)
        if preview.get("ok") is not True or not args.apply:
            result = preview
        else:
            try:
                with sqlite3.connect(shadow_db, timeout=30) as conn:
                    plan = build_p10_shadow_runtime_schema_plan(conn)
                    if plan.get("state") != "ready":
                        result = _blocked("resident_p10_preparer_schema_not_ready")
                    else:
                        persisted = persist_resident_p10_shadow_cycle(
                            conn,
                            prepared_cycle=_mapping(preview.get("prepared_cycle")),
                        )
                        result = {
                            **persisted,
                            "schema": INPUT_SCHEMA,
                            "target_identity": preview.get("target_identity"),
                            "event_date": preview.get("event_date"),
                            "runtime_dependency_identity_sha256": preview.get(
                                "runtime_dependency_identity_sha256"
                            ),
                        }
            except (OSError, sqlite3.Error, ValueError) as exc:
                result = _blocked(f"resident_p10_preparer_persist_failed:{type(exc).__name__}")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
