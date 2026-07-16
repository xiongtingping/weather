#!/usr/bin/env python3
"""Idempotently ensure one future physical-X P0 and resident P10 cycle.

This is a low-frequency preparation stage for the sole monitor daemon, never
an observation hot-path task.  It activates the approved neutral strong
runtime baseline only when the target has no active scorer, builds exactly one
durable P0, and freezes exactly one resident cycle in the isolated shadow DB.
Training/OOS/market bridge coverage is not a live prerequisite.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from initial_posterior_checkpoint_shadow import persist_initial_posterior_checkpoint_shadow  # noqa: E402
from p10_shadow_runtime_schema import build_p10_shadow_runtime_schema_plan  # noqa: E402
from resident_p10_shadow_host import ResidentP10ShadowHost  # noqa: E402
from resident_p10_shadow_prepared_cycle import persist_resident_p10_shadow_cycle  # noqa: E402
from scripts.bootstrap_live_physical_p0_shadow import build_live_physical_p0  # noqa: E402
from scripts.prepare_live_physical_p10_cycle_shadow import build_live_physical_p10_cycle  # noqa: E402
from strong_observation_lr_operational_baseline import build_neutral_strong_lr_operational_baseline  # noqa: E402
from strong_observation_lr_runtime_registry import (  # noqa: E402
    ACTIVATION_ATTESTATION_SCHEMA_VERSION,
    activate_strong_lr_runtime_artifact,
    load_active_strong_lr_runtime_artifact,
    register_strong_lr_runtime_artifact,
)


SCHEMA_VERSION = "next_live_physical_p10_cycle_shadow_ensure_v1"
CONFIRMATION = "ENSURE_NEXT_LIVE_PHYSICAL_P10_CYCLE_SHADOW"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _target(event_date: str) -> str:
    return f"physical_weather_day_v1:ZUUU:{event_date}"


def _activate_baseline_if_missing(
    conn: sqlite3.Connection,
    *,
    target_identity: str,
    activated_at: str,
) -> dict:
    try:
        active = load_active_strong_lr_runtime_artifact(
            conn,
            target_identity=target_identity,
            as_of=activated_at,
        )
        return {"state": "already_active", "artifact_sha256": active.get("artifact_sha256")}
    except ValueError:
        pass
    artifact = build_neutral_strong_lr_operational_baseline()
    reference = "runtime://strong_lr_neutral_operational_baseline_v1"
    registered = register_strong_lr_runtime_artifact(
        conn,
        artifact=artifact,
        registered_at=activated_at,
        registered_by="ssot_probability_daily_preparer",
        registration_basis="approved_neutral_runtime_baseline",
        registration_ref=reference,
    )
    evidence = {
        "schema": ACTIVATION_ATTESTATION_SCHEMA_VERSION,
        "target_identity": target_identity,
        "artifact_sha256": artifact["model_artifact_sha256"],
        "activation_basis": "approved_runtime_baseline",
        "activation_ref": reference,
        "attested_at": activated_at,
        "attested_by": "ssot_probability_daily_preparer",
        "baseline_approval": {
            "approval_kind": "operational_precalibration_baseline",
            "approval_state": "approved",
            "not_trained_or_oos_promoted": True,
            "risk_and_scope_statement": "neutral runtime scorer; zero feature LR and identity latent transition; hard observed-max condition remains composer-owned",
        },
    }
    activated = activate_strong_lr_runtime_artifact(
        conn,
        target_identity=target_identity,
        artifact_sha256=artifact["model_artifact_sha256"],
        activated_at=activated_at,
        activated_by="ssot_probability_daily_preparer",
        activation_basis="approved_runtime_baseline",
        activation_ref=reference,
        activation_evidence=evidence,
    )
    return {"state": "activated", "registered": registered, "activated": activated}


def ensure_next_cycle(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    prepared_at: str,
) -> dict:
    target = _target(event_date)
    existing_cycle = conn.execute(
        """
        SELECT prepared_cycle_id FROM resident_p10_prepared_cycles_shadow
        WHERE target_identity=? ORDER BY prepared_at DESC,rowid DESC LIMIT 1
        """,
        (target,),
    ).fetchone()
    if existing_cycle is not None:
        host = ResidentP10ShadowHost(conn, prepared_cycle_id=str(existing_cycle[0]))
        return {
            "schema": SCHEMA_VERSION,
            "ok": host.readiness().get("ok") is True,
            "state": "already_resident_ready",
            "target_identity": target,
            "prepared_cycle_id": str(existing_cycle[0]),
            "resident_host_readiness": host.readiness(),
        }

    activation = _activate_baseline_if_missing(
        conn,
        target_identity=target,
        activated_at=prepared_at,
    )
    checkpoint = conn.execute(
        "SELECT posterior_checkpoint_id FROM posterior_checkpoints WHERE target_identity=? LIMIT 1",
        (target,),
    ).fetchone()
    p0_state = "already_persisted"
    if checkpoint is None:
        p0 = build_live_physical_p0(
            conn=conn,
            event_date=event_date,
            decision_as_of=prepared_at,
        )
        if p0.get("ok") is not True:
            raise ValueError("daily_physical_p0_blocked:" + ",".join(p0.get("block_reasons") or []))
        persisted_p0 = persist_initial_posterior_checkpoint_shadow(
            conn,
            initial_checkpoint=p0["initial_checkpoint"],
        )
        p0_state = str(persisted_p0.get("state") or "persisted")

    cycle = build_live_physical_p10_cycle(
        conn,
        event_date=event_date,
        prepared_at=prepared_at,
        prepared_by="ssot_probability_daily_preparer",
    )
    if cycle.get("ok") is not True:
        raise ValueError("daily_physical_p10_cycle_blocked:" + ",".join(cycle.get("blockers") or []))
    persisted_cycle = persist_resident_p10_shadow_cycle(
        conn,
        prepared_cycle=cycle["prepared_cycle"],
    )
    cycle_id = str(persisted_cycle.get("prepared_cycle_id") or "")
    host = ResidentP10ShadowHost(conn, prepared_cycle_id=cycle_id)
    return {
        "schema": SCHEMA_VERSION,
        "ok": host.readiness().get("ok") is True,
        "state": "resident_ready",
        "target_identity": target,
        "activation": activation,
        "p0_state": p0_state,
        "prepared_cycle_id": cycle_id,
        "resident_host_readiness": host.readiness(),
        "boundary": {
            "isolated_shadow_only": True,
            "training_or_oos_live_gate": False,
            "market_bridge_live_gate": False,
            "places_orders": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--prepared-at", default="")
    parser.add_argument("--shadow-db", type=Path, default=ROOT / "data" / "shadow" / "p10_runtime.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if not args.apply or args.confirm != CONFIRMATION:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": ["apply_and_confirmation_required"],
        }
    else:
        try:
            with sqlite3.connect(args.shadow_db.expanduser().resolve(), timeout=30) as conn:
                if build_p10_shadow_runtime_schema_plan(conn).get("state") != "ready":
                    raise ValueError("p10_shadow_schema_not_ready")
                conn.execute("BEGIN IMMEDIATE")
                result = ensure_next_cycle(
                    conn,
                    event_date=args.event_date[:10],
                    prepared_at=args.prepared_at or _now(),
                )
                conn.commit()
        except (OSError, sqlite3.Error, ValueError) as exc:
            result = {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "blockers": [str(exc)],
            }
    compact = {
        "schema": result.get("schema"),
        "ok": result.get("ok"),
        "state": result.get("state"),
        "blockers": result.get("blockers") or [],
        "target_identity": result.get("target_identity"),
        "prepared_cycle_id": result.get("prepared_cycle_id"),
        "p0_state": result.get("p0_state"),
        "host_state": (result.get("resident_host_readiness") or {}).get("state"),
        "boundary": result.get("boundary") or {},
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
