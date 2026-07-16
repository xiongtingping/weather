#!/usr/bin/env python3
"""Prove target-level incremental posterior equals a full canonical replay.

Only real canonical events already consumed by the target's latest durable
checkpoint are eligible.  The report is read-only with respect to the shadow
runtime DB and may be materialized only under ``data/research``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sequential_bayes_filter_shadow_runtime import replay_canonical_sequence  # noqa: E402
from sequential_bayes_shadow_pipeline import load_canonical_observation_event  # noqa: E402


SCHEMA_VERSION = "probability_incremental_replay_equivalence_report_v1"
LATEST_SCHEMA_VERSION = "probability_incremental_replay_equivalence_latest_v1"
REPORT_ROOT = ROOT / "data" / "research" / "probability_cutover" / "replay_equivalence"
CONFIRMATION = "I_UNDERSTAND_THIS_WRITES_RESEARCH_EVIDENCE"
BOUND_FILES = (
    "scripts/materialize_probability_replay_equivalence_report.py",
    "sequential_bayes_filter_shadow_runtime.py",
    "sequential_bayes_shadow_pipeline.py",
    "strong_observation_lr_artifact_contract.py",
    "strong_observation_lr_evaluator.py",
    "temperature_trajectory_feature_runtime.py",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    encoded = value if isinstance(value, bytes) else _canonical_json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_json(value: Any, expected: type) -> Any:
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, expected) else None


def _file_bindings(root: Path) -> dict[str, str]:
    return {relative: _sha((root / relative).read_bytes()) for relative in BOUND_FILES}


def _report_sha(payload: Mapping[str, Any]) -> str:
    return _sha({key: value for key, value in payload.items() if key != "report_sha256"})


def _latest_pointer_name(target_identity: str) -> str:
    return f"latest-{_sha(_text(target_identity))[:16]}.json"


def _blocked(target_identity: str, *blockers: str) -> dict[str, Any]:
    payload = {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "target_identity": _text(target_identity),
        "blockers": sorted(set(item for item in blockers if item)),
        "boundary": {
            "reads_runtime_database": True,
            "writes_runtime_database": False,
            "uses_synthetic_events": False,
            "places_orders": False,
        },
    }
    payload["report_sha256"] = _report_sha(payload)
    return payload


def _load_cycle(conn: sqlite3.Connection, target_identity: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT cycle_json FROM resident_p10_prepared_cycles_shadow "
        "WHERE target_identity=? ORDER BY prepared_at DESC,rowid DESC LIMIT 1",
        (_text(target_identity),),
    ).fetchone()
    return _mapping(_parse_json(row[0], dict)) if row else {}


def _latest_incremental_row(conn: sqlite3.Connection, target_identity: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT posterior_checkpoint_id,joint_posterior_sha256,
               physical_high_distribution_json,latent_post_state_json,
               observed_max_lower_bound_c,consumed_event_ids_json,
               forecast_evidence_snapshot_id,prior_context_id,
               resolution_contract_id,model_artifact_sha256,
               canonical_sequence_sha256,checkpoint_kind,committed_at
        FROM posterior_checkpoints
        WHERE target_identity=?
          AND checkpoint_kind='incremental'
        ORDER BY replay_generation DESC,rowid DESC LIMIT 1
        """,
        (_text(target_identity),),
    ).fetchone()


def build_replay_equivalence_report(
    *, shadow_db: Path, target_identity: str, root: Path = ROOT,
) -> dict[str, Any]:
    target = _text(target_identity)
    try:
        with sqlite3.connect(f"file:{shadow_db.resolve()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cycle = _load_cycle(conn, target)
            if not cycle:
                return _blocked(target, "replay_prepared_cycle_missing")
            incremental = _latest_incremental_row(conn, target)
            if incremental is None:
                return _blocked(target, "replay_real_incremental_checkpoint_missing")
            consumed_ids = _parse_json(incremental["consumed_event_ids_json"], list)
            if not consumed_ids:
                return _blocked(target, "replay_consumed_canonical_sequence_empty")
            events: list[dict[str, Any]] = []
            for event_id in consumed_ids:
                loaded = load_canonical_observation_event(
                    conn, canonical_observation_event_id=_text(event_id),
                )
                if loaded.get("ok") is not True:
                    return _blocked(target, "replay_consumed_canonical_event_unavailable")
                events.append(_mapping(loaded.get("canonical_observation_event")))
            placeholders = ",".join("?" for _ in consumed_ids)
            update_rows = conn.execute(
                f"""
                SELECT strong_observation_update_id,canonical_observation_event_id,
                       decision_as_of,model_artifact_sha256,
                       raw_log_score_sha256,posterior_delta_sha256
                FROM strong_observation_updates
                WHERE target_identity=?
                  AND canonical_observation_event_id IN ({placeholders})
                ORDER BY decision_as_of,canonical_observation_event_id
                """,
                (target, *[_text(item) for item in consumed_ids]),
            ).fetchall()
            if len(update_rows) != len(consumed_ids):
                return _blocked(target, "replay_strong_update_one_to_one_failed")
            decision_as_of = max(_text(row["decision_as_of"]) for row in update_rows)
            if not decision_as_of:
                return _blocked(target, "replay_decision_asof_missing")
            posterior = _mapping(cycle.get("posterior_context"))
            initial = _mapping(cycle.get("previous_checkpoint"))
            artifact = _mapping(posterior.get("approved_lr_artifact"))
            resolution = _mapping(posterior.get("resolution_contract"))
            replay = replay_canonical_sequence(
                initial_checkpoint=initial,
                canonical_events=events,
                forecast_evidence_snapshot_id=_text(posterior.get("forecast_evidence_snapshot_id")),
                prior_context_id=_text(posterior.get("prior_context_id")),
                approved_lr_artifact=artifact,
                decision_as_of=decision_as_of,
                deadline_at=decision_as_of,
                fact_snapshot_id_prefix=f"replay:{target}",
                resolution_contract_id=_text(resolution.get("resolution_contract_id")),
                source_reliability_pre_state=_mapping(posterior.get("source_reliability_before")),
                temperature_quantization_c=float(posterior.get("temperature_quantization_c") or 1.0),
            )
            if replay.get("ok") is not True:
                return _blocked(target, f"replay_runtime_blocked:{_text(replay.get('reason')) or 'unknown'}")
            replay_checkpoint = _mapping(replay.get("checkpoint"))
            persisted_physical = _parse_json(incremental["physical_high_distribution_json"], dict)
            persisted_latent = _parse_json(incremental["latent_post_state_json"], dict)
            comparisons = {
                "joint_posterior_sha256": _text(replay_checkpoint.get("joint_posterior_sha256")) == _text(incremental["joint_posterior_sha256"]),
                "physical_high_distribution": replay_checkpoint.get("physical_high_distribution") == persisted_physical,
                "latent_post_state_distribution": replay_checkpoint.get("latent_post_state_distribution") == persisted_latent,
                "consumed_event_ids": replay_checkpoint.get("consumed_event_ids") == consumed_ids,
                "observed_max_lower_bound_c": replay_checkpoint.get("observed_max_lower_bound_c") == incremental["observed_max_lower_bound_c"],
                "forecast_evidence_snapshot_id": _text(replay_checkpoint.get("forecast_evidence_snapshot_id")) == _text(incremental["forecast_evidence_snapshot_id"]),
                "prior_context_id": _text(replay_checkpoint.get("prior_context_id")) == _text(incremental["prior_context_id"]),
                "resolution_contract_id": _text(replay_checkpoint.get("resolution_contract_id")) == _text(incremental["resolution_contract_id"]),
                "model_artifact_sha256": _text(replay_checkpoint.get("model_artifact_sha256")) == _text(incremental["model_artifact_sha256"]),
            }
            ok = all(comparisons.values())
            payload: dict[str, Any] = {
                "schema": SCHEMA_VERSION,
                "ok": ok,
                "state": "equivalent_current_target_snapshot" if ok else "not_equivalent",
                "target_identity": target,
                "executed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "blockers": [] if ok else [f"replay_comparison_failed:{key}" for key, value in comparisons.items() if not value],
                "comparisons": comparisons,
                "bound_snapshot": {
                    "prepared_cycle_id": _text(cycle.get("prepared_cycle_id")),
                    "prepared_cycle_sha256": _text(cycle.get("prepared_cycle_sha256")),
                    "initial_checkpoint_id": _text(initial.get("posterior_checkpoint_id")),
                    "initial_posterior_sha256": _text(initial.get("joint_posterior_sha256")),
                    "incremental_checkpoint_id": _text(incremental["posterior_checkpoint_id"]),
                    "incremental_posterior_sha256": _text(incremental["joint_posterior_sha256"]),
                    "incremental_committed_at": _text(incremental["committed_at"]),
                    "canonical_event_ids": consumed_ids,
                    "canonical_event_payload_sha256": _sha(events),
                    "strong_update_bindings": [dict(row) for row in update_rows],
                    "strong_update_bindings_sha256": _sha([dict(row) for row in update_rows]),
                },
                "replay_result": {
                    "joint_posterior_sha256": replay_checkpoint.get("joint_posterior_sha256"),
                    "canonical_sequence_sha256": replay.get("canonical_sequence_sha256"),
                    "consumed_event_ids": replay_checkpoint.get("consumed_event_ids"),
                },
                "file_sha256": _file_bindings(root),
                "boundary": {
                    "reads_runtime_database": True,
                    "writes_runtime_database": False,
                    "real_consumed_canonical_events_only": True,
                    "uses_synthetic_events": False,
                    "activates_model": False,
                    "places_orders": False,
                    "proves_live_latency": False,
                },
            }
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return _blocked(target, f"replay_report_build_failed:{type(exc).__name__}")
    payload["report_sha256"] = _report_sha(payload)
    return payload


def validate_report(
    payload: Mapping[str, Any], *, shadow_db: Path, target_identity: str, root: Path = ROOT,
) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema") != SCHEMA_VERSION:
        blockers.append("replay_report_schema_invalid")
    if _text(payload.get("target_identity")) != _text(target_identity):
        blockers.append("replay_report_target_mismatch")
    if _text(payload.get("report_sha256")) != _report_sha(payload):
        blockers.append("replay_report_sha256_mismatch")
    if payload.get("ok") is not True or not all((_mapping(payload.get("comparisons"))).values()):
        blockers.append("replay_report_not_equivalent")
    try:
        if _mapping(payload.get("file_sha256")) != _file_bindings(root):
            blockers.append("replay_report_stale_for_current_code")
        with sqlite3.connect(f"file:{shadow_db.resolve()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            row = _latest_incremental_row(conn, target_identity)
            snapshot = _mapping(payload.get("bound_snapshot"))
            if row is None:
                blockers.append("replay_current_incremental_checkpoint_missing")
            elif (
                _text(snapshot.get("incremental_checkpoint_id")) != _text(row["posterior_checkpoint_id"])
                or _text(snapshot.get("incremental_posterior_sha256")) != _text(row["joint_posterior_sha256"])
                or list(snapshot.get("canonical_event_ids") or []) != (_parse_json(row["consumed_event_ids_json"], list) or [])
            ):
                blockers.append("replay_report_stale_for_current_target_snapshot")
    except (OSError, sqlite3.Error):
        blockers.append("replay_report_current_binding_unreadable")
    boundary = _mapping(payload.get("boundary"))
    if not (
        boundary.get("writes_runtime_database") is False
        and boundary.get("real_consumed_canonical_events_only") is True
        and boundary.get("uses_synthetic_events") is False
        and boundary.get("places_orders") is False
    ):
        blockers.append("replay_report_boundary_invalid")
    return sorted(set(blockers))


def load_verified_latest_report(
    *, shadow_db: Path, target_identity: str, report_root: Path = REPORT_ROOT, root: Path = ROOT,
) -> tuple[dict[str, Any], str, list[str]]:
    pointer = report_root / _latest_pointer_name(target_identity)
    try:
        latest = json.loads(pointer.read_text(encoding="utf-8"))
        if latest.get("schema") != LATEST_SCHEMA_VERSION:
            return {}, "", ["replay_latest_pointer_schema_invalid"]
        report_sha = _text(latest.get("report_sha256"))
        report_path = (report_root / f"{report_sha}.json").resolve()
        if report_path.parent != report_root.resolve():
            return {}, "", ["replay_latest_pointer_path_invalid"]
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "", ["replay_equivalence_report_missing"]
    except (OSError, ValueError, TypeError):
        return {}, "", ["replay_equivalence_report_unreadable"]
    blockers = validate_report(payload, shadow_db=shadow_db, target_identity=target_identity, root=root)
    if _text(payload.get("report_sha256")) != report_sha:
        blockers.append("replay_latest_pointer_sha_mismatch")
    return dict(payload), str(report_path), sorted(set(blockers))


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-identity", required=True)
    parser.add_argument("--shadow-db", type=Path, default=ROOT / "data" / "shadow" / "p10_runtime.db")
    parser.add_argument("--report-root", type=Path, default=REPORT_ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    shadow_db = args.shadow_db.expanduser().resolve()
    report = build_replay_equivalence_report(shadow_db=shadow_db, target_identity=args.target_identity)
    if args.apply:
        if args.confirm != CONFIRMATION:
            raise ValueError("research_evidence_write_confirmation_missing")
        if report.get("ok") is not True:
            raise ValueError("cannot_materialize_blocked_replay_report")
        report_root = args.report_root.expanduser().resolve()
        allowed = (ROOT / "data" / "research").resolve()
        if report_root != allowed and allowed not in report_root.parents:
            raise ValueError("replay_report_path_outside_research_root")
        report_path = report_root / f"{report['report_sha256']}.json"
        encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if report_path.exists() and report_path.read_text(encoding="utf-8") != encoded:
            raise ValueError("replay_immutable_report_collision")
        if not report_path.exists():
            _atomic_write(report_path, report)
        _atomic_write(
            report_root / _latest_pointer_name(args.target_identity),
            {
                "schema": LATEST_SCHEMA_VERSION,
                "target_identity": args.target_identity,
                "report_sha256": report["report_sha256"],
                "report_path": str(report_path),
                "state": report["state"],
            },
        )
        report["materialized_path"] = str(report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
