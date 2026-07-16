#!/usr/bin/env python3
"""Assemble and persist the resident physical-X P10 cycle from durable P0.

Only the isolated shadow database is touched.  The cycle binds the latest
durable checkpoint, its immutable prior/weak lineage, the registry-selected
strong scorer, and the physical-only resolution contract.  No forecast fetch,
training/OOS gate, market dependency, strategy, signer, CLOB or order exists
on this path.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from p10_shadow_runtime_schema import build_p10_shadow_runtime_schema_plan  # noqa: E402
from resident_p10_shadow_host import (  # noqa: E402
    ResidentP10ShadowHost,
    load_latest_persisted_checkpoint,
)
from resident_p10_shadow_prepared_cycle import (  # noqa: E402
    freeze_resident_p10_shadow_cycle,
    persist_resident_p10_shadow_cycle,
)
from scripts.bootstrap_live_physical_p0_shadow import (  # noqa: E402
    DEFAULT_RESOLUTION_AUTHORITY,
    build_physical_only_resolution_contract,
)
from strong_observation_lr_runtime_registry import load_active_strong_lr_runtime_artifact  # noqa: E402


SCHEMA_VERSION = "live_physical_p10_cycle_shadow_preparer_v1"
CONFIRMATION = "PERSIST_LIVE_PHYSICAL_P10_CYCLE_SHADOW"
LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _event_valid_until(event_date: str) -> str:
    local_day = date.fromisoformat(event_date)
    local_end = datetime.combine(local_day, time(23, 59, 59), tzinfo=LOCAL_ZONE)
    return local_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _loads_mapping(value: Any, *, error: str) -> dict[str, Any]:
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(error) from exc
    if not isinstance(parsed, Mapping):
        raise ValueError(error)
    return dict(parsed)


def _realization_policy() -> dict[str, Any]:
    core = {
        "schema": "realization_state_policy_v2",
        "approval_state": "approved",
        "credible_probability_floor": 0.05,
        "strong_support_log_probability_ratio_floor": 0.0,
        "strong_exclusion_probability_ceiling": 0.01,
        "strong_exclusion_log_probability_ratio_ceiling": -1.0,
        "scope": "physical_posterior_cycle_market_projection_na",
    }
    return {**core, "artifact_sha256": _sha(core)}


def build_live_physical_p10_cycle(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    prepared_at: str,
    prepared_by: str,
) -> dict[str, Any]:
    target = f"physical_weather_day_v1:ZUUU:{event_date}"
    previous = load_latest_persisted_checkpoint(
        conn,
        target_identity=target,
        event_date=event_date,
    )
    if not previous:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": ["live_physical_p10_persisted_p0_missing"],
        }
    lineage_row = conn.execute(
        """
        SELECT prior_context_id,prior_artifact_sha256,weak_likelihood_artifact_sha256,
               forecast_evidence_snapshot_id,strong_lr_artifact_sha256,resolution_contract_id
        FROM initial_posterior_lineage_shadow WHERE posterior_checkpoint_id=?
        """,
        (_text(previous.get("posterior_checkpoint_id")),),
    ).fetchone()
    checkpoint_row = conn.execute(
        """
        SELECT source_reliability_state_json FROM posterior_checkpoints
        WHERE posterior_checkpoint_id=?
        """,
        (_text(previous.get("posterior_checkpoint_id")),),
    ).fetchone()
    if lineage_row is None or checkpoint_row is None:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": ["live_physical_p10_p0_lineage_missing"],
        }
    resolution = build_physical_only_resolution_contract(
        DEFAULT_RESOLUTION_AUTHORITY,
        event_date=event_date,
    )
    if _text(lineage_row[5]) != _text(resolution.get("resolution_contract_id")):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": ["live_physical_p10_resolution_contract_drift"],
        }
    active = load_active_strong_lr_runtime_artifact(
        conn,
        target_identity=target,
        as_of=prepared_at,
    )
    artifact = _mapping(active.get("artifact"))
    if _text(active.get("artifact_sha256")) != _text(lineage_row[4]):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": ["live_physical_p10_active_strong_artifact_p0_mismatch"],
        }
    reliability = _loads_mapping(
        checkpoint_row[0],
        error="live_physical_p10_source_reliability_invalid",
    )
    valid_until = _event_valid_until(event_date)
    cycle_input = {
        "target_identity": target,
        "event_date": event_date,
        "public_slug": f"physical-weather-day-zuuu-{event_date}",
        "previous_checkpoint": previous,
        "prewarmed_components": {
            "forecast_snapshot": {
                "ready": True,
                "forecast_snapshot_id": _text(lineage_row[3]),
                "artifact_sha256": _text(lineage_row[2]),
                "valid_until": valid_until,
            },
            "prior_context": {
                "ready": True,
                "prior_context_id": _text(lineage_row[0]),
                "artifact_sha256": _text(lineage_row[1]),
                "valid_until": valid_until,
            },
            "strong_lr_artifact": {
                "ready": True,
                "approved": True,
                "strong_lr_artifact_sha256": _text(active.get("artifact_sha256")),
                "valid_until": valid_until,
            },
        },
        "posterior_context": {
            "forecast_evidence_snapshot_id": _text(lineage_row[3]),
            "prior_context_id": _text(lineage_row[0]),
            "approved_lr_artifact": artifact,
            "resolution_contract": resolution,
            "realization_policy": _realization_policy(),
            "temperature_quantization_c": 0.25,
            "source_reliability_before": reliability,
            "source_reliability_after": reliability,
            "path_auxiliary": {
                "shadow_only": True,
                "market_projection_na": True,
                "strict_asof_oos_not_live_gate": True,
            },
        },
        "decision_context": {},
    }
    frozen = freeze_resident_p10_shadow_cycle(
        cycle_input,
        prepared_at=prepared_at,
        prepared_by=prepared_by,
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": frozen.get("ok") is True,
        "state": frozen.get("state"),
        "blockers": list(frozen.get("blockers") or []),
        "target_identity": target,
        "event_date": event_date,
        "prepared_cycle": frozen.get("prepared_cycle"),
        "validation": frozen.get("validation"),
        "lineage": {
            "previous_checkpoint_id": previous.get("posterior_checkpoint_id"),
            "forecast_evidence_snapshot_id": _text(lineage_row[3]),
            "weak_likelihood_artifact_sha256": _text(lineage_row[2]),
            "prior_context_id": _text(lineage_row[0]),
            "prior_artifact_sha256": _text(lineage_row[1]),
            "strong_lr_artifact_sha256": _text(lineage_row[4]),
            "resolution_contract_id": _text(lineage_row[5]),
        },
        "boundary": {
            "shadow_only": True,
            "market_context_required": False,
            "training_or_oos_live_gate": False,
            "places_orders": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--prepared-at", default="")
    parser.add_argument("--prepared-by", default="codex:ssot_probability_mainline")
    parser.add_argument("--shadow-db", type=Path, default=ROOT / "data" / "shadow" / "p10_runtime.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRMATION:
        result = {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["confirmation_phrase_required"]}
    else:
        try:
            with sqlite3.connect(args.shadow_db.expanduser().resolve(), timeout=30) as conn:
                if build_p10_shadow_runtime_schema_plan(conn).get("state") != "ready":
                    raise ValueError("p10_shadow_schema_not_ready")
                result = build_live_physical_p10_cycle(
                    conn,
                    event_date=args.event_date[:10],
                    prepared_at=args.prepared_at or _now(),
                    prepared_by=args.prepared_by,
                )
                if args.apply and result.get("ok") is True:
                    conn.execute("BEGIN IMMEDIATE")
                    persistence = persist_resident_p10_shadow_cycle(
                        conn,
                        prepared_cycle=_mapping(result.get("prepared_cycle")),
                    )
                    conn.commit()
                    cycle_id = _text(persistence.get("prepared_cycle_id"))
                    host = ResidentP10ShadowHost(conn, prepared_cycle_id=cycle_id)
                    result["persistence"] = persistence
                    result["resident_host_readiness"] = host.readiness()
                    result["state"] = "resident_ready"
        except (OSError, sqlite3.Error, ValueError) as exc:
            result = {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "blockers": [str(exc)],
            }
    compact = {
        key: result.get(key)
        for key in (
            "schema", "ok", "state", "blockers", "target_identity", "event_date",
            "lineage", "persistence", "resident_host_readiness", "boundary",
        )
        if result.get(key) not in (None, [], {})
    }
    if result.get("prepared_cycle"):
        cycle = _mapping(result.get("prepared_cycle"))
        compact["prepared_cycle_id"] = cycle.get("prepared_cycle_id")
        compact["prepared_cycle_sha256"] = cycle.get("prepared_cycle_sha256")
    print(json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
