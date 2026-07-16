#!/usr/bin/env python3
"""Build or persist one P0 prior×weak checkpoint in an isolated shadow DB.

The input JSON must contain only the P0 contract fields.  The default action
is pure construction/validation; ``--apply`` is additionally protected by an
explicit confirmation and refuses both production archive.db and a shadow DB
whose P10 schema has not already been initialized.
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

from initial_posterior_checkpoint_shadow import (  # noqa: E402
    build_initial_posterior_checkpoint,
    persist_initial_posterior_checkpoint_shadow,
)
from p10_shadow_runtime_schema import build_p10_shadow_runtime_schema_plan  # noqa: E402
from strong_observation_lr_runtime_registry import load_active_strong_lr_runtime_artifact  # noqa: E402


CONFIRMATION = "PERSIST_INITIAL_POSTERIOR_SHADOW"
REQUIRED_INPUT_KEYS = (
    "target_identity",
    "event_date",
    "station_id",
    "decision_as_of",
    "prior_context",
    "joint_weak_likelihood",
    "resolution_contract",
    "source_reliability_initial_state",
)


def _blocked(*blockers: str) -> dict[str, Any]:
    return {
        "ok": False,
        "state": "blocked",
        "blockers": sorted(set(blocker for blocker in blockers if blocker)),
        "shadow_only": True,
        "places_orders": False,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _load_input(path: Path) -> tuple[Mapping[str, Any], list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, ["initial_posterior_input_json_unreadable"]
    payload = _mapping(value)
    if not payload:
        return {}, ["initial_posterior_input_json_not_object"]
    missing = [f"initial_posterior_input_{key}_missing" for key in REQUIRED_INPUT_KEYS if key not in payload]
    if "active_strong_lr_artifact" in payload:
        missing.append("initial_posterior_input_caller_supplied_strong_artifact_forbidden")
    return payload, missing


def _build(payload: Mapping[str, Any], *, active_strong_lr_artifact: Mapping[str, Any]) -> dict[str, Any]:
    return build_initial_posterior_checkpoint(
        target_identity=str(payload.get("target_identity") or ""),
        event_date=str(payload.get("event_date") or ""),
        station_id=str(payload.get("station_id") or ""),
        decision_as_of=str(payload.get("decision_as_of") or ""),
        prior_context=_mapping(payload.get("prior_context")),
        joint_weak_likelihood=_mapping(payload.get("joint_weak_likelihood")),
        active_strong_lr_artifact=active_strong_lr_artifact,
        resolution_contract=_mapping(payload.get("resolution_contract")),
        source_reliability_initial_state=_mapping(payload.get("source_reliability_initial_state")),
    )


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
        result = _blocked("initial_posterior_shadow_confirmation_invalid")
    elif shadow_db.name == "archive.db":
        result = _blocked("production_archive_db_forbidden")
    else:
        payload, blockers = _load_input(input_path)
        if blockers:
            result = _blocked(*blockers)
        else:
            try:
                connection_target = shadow_db if args.apply else f"file:{shadow_db}?mode=ro"
                with sqlite3.connect(connection_target, timeout=30, uri=not args.apply) as conn:
                    plan = build_p10_shadow_runtime_schema_plan(conn)
                    if plan.get("state") != "ready":
                        result = _blocked("initial_posterior_shadow_schema_not_ready")
                    else:
                        try:
                            active = load_active_strong_lr_runtime_artifact(
                                conn,
                                target_identity=str(payload.get("target_identity") or ""),
                                as_of=str(payload.get("decision_as_of") or ""),
                            )
                        except ValueError:
                            result = _blocked("initial_posterior_active_strong_lr_artifact_unavailable")
                        else:
                            initial = _build(
                                payload,
                                active_strong_lr_artifact=_mapping(active.get("artifact")),
                            )
                            if initial.get("ok") is not True:
                                result = initial
                            elif not args.apply:
                                result = {
                                    "ok": True,
                                    "state": "preview_ready",
                                    "posterior_checkpoint_id": initial["checkpoint"]["posterior_checkpoint_id"],
                                    "initial_evidence_sha256": initial["initial_lineage"]["initial_evidence_sha256"],
                                    "strong_lr_activation_id": str(active.get("activation_id") or ""),
                                    "strong_lr_artifact_sha256": str(active.get("artifact_sha256") or ""),
                                    "shadow_only": True,
                                    "read_only": True,
                                    "places_orders": False,
                                }
                            else:
                                result = persist_initial_posterior_checkpoint_shadow(
                                    conn, initial_checkpoint=initial
                                )
            except (OSError, sqlite3.Error, ValueError) as exc:
                result = _blocked(f"initial_posterior_shadow_persist_failed:{type(exc).__name__}")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
