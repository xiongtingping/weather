#!/usr/bin/env python3
"""Build and persist the live physical-X P0 in isolated shadow.

The assembler owns the canonical runtime inputs: versioned physical X grid,
approved NCEI climate-reference baseline, neutral Z0 runtime baseline, unique
joint weak model, registry-selected strong scorer, and physical-only resolution
projection while X->S->B is still open.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from initial_posterior_checkpoint_shadow import build_initial_posterior_checkpoint, persist_initial_posterior_checkpoint_shadow  # noqa: E402
from joint_weak_live_evidence_runtime import build_live_joint_weak_likelihood  # noqa: E402
from p10_shadow_runtime_schema import build_p10_shadow_runtime_schema_plan  # noqa: E402
from physical_latent_state_prior_runtime import build_neutral_physical_latent_state_operational_baseline  # noqa: E402
from physical_temperature_prior_context_runtime import (  # noqa: E402
    build_full_physical_p0_prior_context,
    build_ncei_operational_climate_reference_prior_context,
)
from physical_x_bucket_contract_runtime import build_physical_x_bucket_contract  # noqa: E402
from strong_observation_lr_runtime_registry import load_active_strong_lr_runtime_artifact  # noqa: E402


SCHEMA_VERSION = "live_physical_p0_shadow_bootstrap_v1"
CONFIRMATION = "PERSIST_LIVE_PHYSICAL_P0_SHADOW"
DEFAULT_NCEI_ROOT = ROOT / "data" / "research" / "ncei_zuuu_joint_model" / "2021_2025"
DEFAULT_RESOLUTION_AUTHORITY = ROOT / "config" / "resolution_authorities" / "zuuu_amsc_wunderground_daily_high_v4.json"


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_physical_only_resolution_contract(path: Path, *, event_date: str) -> dict[str, Any]:
    authority = json.loads(path.read_text(encoding="utf-8"))
    source = authority.get("settlement_s") if isinstance(authority.get("settlement_s"), dict) else {}
    core = {
        "authority_contract_id": str(authority.get("contract_id") or ""),
        "authority_contract_sha256": _sha(authority),
        "resolution_source_id": str(source.get("source_id") or ""),
        "resolution_local_date": event_date,
        "settlement_unit": "C",
        "bridge_closed": False,
        "validated": False,
        "market_bucket_probabilities_na_reason": "x_to_s_to_b_bridge_data_accumulation",
    }
    return {**core, "resolution_contract_id": f"physicalonly_{_sha(core)[:24]}"}


def build_live_physical_p0(*, conn: sqlite3.Connection, event_date: str, decision_as_of: str) -> dict[str, Any]:
    target_identity = f"physical_weather_day_v1:ZUUU:{event_date}"
    grid_contract = build_physical_x_bucket_contract()
    if grid_contract.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": list(grid_contract.get("block_reasons") or [])}
    grid = grid_contract["temperature_grid_c"]
    temperature_prior = build_ncei_operational_climate_reference_prior_context(
        event_date=event_date,
        decision_as_of=decision_as_of,
        temperature_grid_c=grid,
        daily_labels_path=DEFAULT_NCEI_ROOT / "derived" / "daily_labels.csv",
        download_manifest_path=DEFAULT_NCEI_ROOT / "download_manifest.json",
    )
    latent_prior = build_neutral_physical_latent_state_operational_baseline(temperature_grid_c=grid)
    prior = build_full_physical_p0_prior_context(
        temperature_prior_context=temperature_prior,
        latent_state_prior=latent_prior,
    )
    weak_envelope = build_live_joint_weak_likelihood(
        temperature_grid_c=grid,
        event_date=event_date,
        decision_as_of=decision_as_of,
    )
    try:
        active = load_active_strong_lr_runtime_artifact(
            conn,
            target_identity=target_identity,
            as_of=decision_as_of,
        )
    except ValueError:
        active = {}
    blockers: list[str] = []
    if prior.get("ok") is not True:
        blockers.extend(prior.get("block_reasons") or ["live_p0_prior_unavailable"])
    if weak_envelope.get("ok") is not True:
        blockers.extend(weak_envelope.get("block_reasons") or ["live_p0_weak_unavailable"])
    if not active:
        blockers.append("live_p0_active_strong_lr_unavailable")
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers)),
            "temperature_prior": temperature_prior,
            "latent_prior": latent_prior,
            "prior_context": prior,
            "weak_likelihood": weak_envelope,
            "boundary": {"shadow_only": True, "places_orders": False},
        }
    resolution = build_physical_only_resolution_contract(DEFAULT_RESOLUTION_AUTHORITY, event_date=event_date)
    initial = build_initial_posterior_checkpoint(
        target_identity=target_identity,
        event_date=event_date,
        station_id="ZUUU",
        decision_as_of=decision_as_of,
        prior_context=prior,
        joint_weak_likelihood=weak_envelope["joint_weak_likelihood"],
        active_strong_lr_artifact=active["artifact"],
        resolution_contract=resolution,
        source_reliability_initial_state={
            "amsc_zuuu_scheduled_metar_daily_max_v2": {"state": "unknown"},
            "awc_zuuu_metar_crosscheck": {"state": "unknown"},
        },
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": initial.get("ok") is True,
        "state": initial.get("state"),
        "block_reasons": list(initial.get("block_reasons") or []),
        "target_identity": target_identity,
        "decision_as_of": decision_as_of,
        "temperature_grid_contract": grid_contract,
        "temperature_prior": temperature_prior,
        "latent_prior": latent_prior,
        "prior_context": prior,
        "weak_likelihood": weak_envelope,
        "active_strong_lr": {key: active.get(key) for key in ("activation_id", "artifact_sha256", "activated_at", "activation_basis")},
        "resolution_contract": resolution,
        "initial_checkpoint": initial,
        "boundary": {
            "shadow_only": True,
            "strict_asof_oos_not_live_gate": True,
            "x_to_s_to_b_bridge_not_p0_x_gate": True,
            "writes_archive_db": False,
            "places_orders": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--decision-as-of", default="")
    parser.add_argument("--shadow-db", type=Path, default=ROOT / "data" / "shadow" / "p10_runtime.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    as_of = args.decision_as_of or _now()
    if args.apply and args.confirm != CONFIRMATION:
        print(json.dumps({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["confirmation_phrase_required"]}, ensure_ascii=False, indent=2))
        return 2
    try:
        with sqlite3.connect(args.shadow_db.expanduser().resolve(), timeout=30) as conn:
            if build_p10_shadow_runtime_schema_plan(conn).get("state") != "ready":
                raise ValueError("p10_shadow_schema_not_ready")
            result = build_live_physical_p0(conn=conn, event_date=args.event_date[:10], decision_as_of=as_of)
            if args.apply and result.get("ok") is True:
                conn.execute("BEGIN IMMEDIATE")
                persisted = persist_initial_posterior_checkpoint_shadow(
                    conn,
                    initial_checkpoint=result["initial_checkpoint"],
                )
                conn.commit()
                result["persistence"] = persisted
                result["state"] = "persisted"
    except (OSError, json.JSONDecodeError, sqlite3.Error, ValueError) as exc:
        result = {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
