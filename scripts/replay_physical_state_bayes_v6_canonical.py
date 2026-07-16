#!/usr/bin/env python3
"""Deterministic, read-only canonical replay for the production v6 cutover."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from physical_state_bayes_v6.artifact_contracts import validate_observation_artifact, validate_transition_artifact
from physical_state_bayes_v6.contracts import JointCell
from production_probability_v6_activation_contract import validate_v6_activation
from production_probability_v6_event_runtime import compose_v6_canonical_event
from production_probability_v6_p0_runtime import compile_v6_p0_candidate
from production_probability_v6_runtime_manifest import v6_runtime_manifest_sha256


SCHEMA_VERSION = "physical_state_bayes_v6_canonical_production_replay_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("v6_replay_naive_time")
    return parsed.astimezone(timezone.utc)


def _canonical_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    try:
        value["parsed_observation"] = json.loads(_text(value.get("parsed_observation_json")) or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("v6_replay_canonical_payload_invalid") from exc
    return value


def _initial_joint(checkpoint: Mapping[str, Any]) -> tuple[JointCell, ...]:
    rows = json.loads(_text(checkpoint.get("joint_posterior_json")) or "[]")
    result = []
    for row in rows:
        probability = float(row.get("probability") or 0.0)
        if probability <= 0.0:
            continue
        result.append(
            JointCell(
                y_c=float(row.get("continuous_y_c", row.get("temperature_c"))),
                state=_text(row.get("state")),
                probability=probability,
            )
        )
    total = sum(row.probability for row in result)
    if not result or total <= 0.0:
        raise ValueError("v6_replay_initial_joint_missing")
    return tuple(JointCell(row.y_c, row.state, row.probability / total) for row in result)


def _missing_forcing_weak(activation: Mapping[str, Any], y_grid: list[float]) -> dict[str, Any]:
    paths = []
    responsibilities = [
        {
            "continuous_y_c": y_c,
            "scenarios": [{"scenario_id": "historical_curve_not_archived", "responsibility": 1.0}],
        }
        for y_c in y_grid
    ]
    return {
        "artifact_sha256": _text(activation.get("weak_likelihood_artifact_sha256")),
        "joint_forecast_weak_likelihood_sha256": _sha({"y_grid": y_grid}),
        "forecast_scenario_paths": {
            "sha256": _sha(paths),
            "paths": paths,
            "historical_forcing_availability": "unavailable_explicit_not_zero_weather",
        },
        "candidate_scenario_responsibilities": {
            "sha256": _sha(responsibilities),
            "rows": responsibilities,
        },
    }


def _replay_target(
    *,
    activation: Mapping[str, Any],
    transition: Mapping[str, Any],
    observation: Mapping[str, Any],
    source_p0: Mapping[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    if not events:
        raise ValueError("v6_replay_target_events_missing")
    initial_joint = _initial_joint(source_p0)
    y_grid = sorted({row.y_c for row in initial_joint})
    weak = _missing_forcing_weak(activation, y_grid)
    first_valid = _time(events[0]["valid_time"])
    reference = (first_valid - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    target = _text(source_p0.get("target_identity"))
    p0 = compile_v6_p0_candidate(
        activation=activation,
        target_identity=target,
        event_date=target.rsplit(":", 1)[-1],
        decision_as_of=_text(source_p0.get("created_at")),
        state_reference_valid_at=reference,
        prior_context_id=_text(source_p0.get("prior_context_id")),
        prior_model_artifact_sha256=_text(activation.get("prior_model_artifact_sha256")),
        forecast_evidence_snapshot_id=_text(source_p0.get("forecast_evidence_snapshot_id")) or "legacy-p0-no-snapshot",
        weak_likelihood_artifact_sha256=_text(activation.get("weak_likelihood_artifact_sha256")),
        resolution_contract_id=_text(source_p0.get("resolution_contract_id")),
        forcing_generation_sha256=weak["forecast_scenario_paths"]["sha256"],
        initial_joint=initial_joint,
        replay_generation=int(source_p0.get("replay_generation") or 0) + 1,
        supersedes_checkpoint_id=_text(source_p0.get("posterior_checkpoint_id")),
    )
    checkpoint = p0["checkpoint_row"]
    history: list[dict[str, Any]] = []
    checkpoint_hashes = []
    latencies_ms = []
    hard_bounds = []
    for event in events:
        started = time.perf_counter()
        result = compose_v6_canonical_event(
            activation=activation,
            previous_checkpoint=checkpoint,
            canonical_event=event,
            pre_event_history=history,
            weak_likelihood=weak,
            transition_artifact=transition,
            observation_artifact=observation,
            decision_as_of=_text(event.get("first_actionable_seen_at")),
            completed_at=_text(event.get("first_actionable_seen_at")),
        )
        latencies_ms.append((time.perf_counter() - started) * 1000.0)
        if result.get("current_observation_consumed_once") is not True:
            raise ValueError("v6_replay_current_observation_not_consumed_once")
        if result.get("nwp_hourly_path_likelihood_reapplied") is not False:
            raise ValueError("v6_replay_nwp_duplicate_likelihood")
        checkpoint = result["checkpoint_row"]
        checkpoint_hashes.append(_text(checkpoint.get("joint_posterior_sha256")))
        hard_bounds.append(checkpoint.get("reported_x_lower_bound_c"))
        history.append(event)
    consumed = json.loads(_text(checkpoint.get("consumed_event_ids_json")) or "[]")
    if consumed != [_text(event.get("canonical_observation_event_id")) for event in events]:
        raise ValueError("v6_replay_consumed_sequence_mismatch")
    return {
        "target_identity": target,
        "source_p0_checkpoint_id": _text(source_p0.get("posterior_checkpoint_id")),
        "source_p0_created_at": _text(source_p0.get("created_at")),
        "event_count": len(events),
        "first_event_valid_at": _text(events[0].get("valid_time")),
        "last_event_valid_at": _text(events[-1].get("valid_time")),
        "canonical_sequence_sha256": _text(checkpoint.get("canonical_sequence_sha256")),
        "checkpoint_hashes": checkpoint_hashes,
        "final_joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
        "final_reported_x_distribution": json.loads(checkpoint["reported_physical_x_distribution_json"]),
        "final_reported_x_lower_bound_c": checkpoint.get("reported_x_lower_bound_c"),
        "hard_bounds_monotone": all(
            right is None or left is None or int(right) >= int(left)
            for left, right in zip(hard_bounds, hard_bounds[1:])
        ),
        "max_event_compute_ms": max(latencies_ms),
        "p95_event_compute_ms": sorted(latencies_ms)[max(0, math.ceil(0.95 * len(latencies_ms)) - 1)],
        "historical_hourly_forcing": "explicitly_unavailable",
    }


def audit(root: Path, db: Path) -> dict[str, Any]:
    activation = validate_v6_activation(
        json.loads((root / "config/production_probability_runtime_activation.v6_candidate.json").read_text())
    )
    transition = validate_transition_artifact(
        json.loads((root / "config/physical_state_bayes_v6_transition_artifact.json").read_text())
    )
    observation = validate_observation_artifact(
        json.loads((root / "config/physical_state_bayes_v6_observation_artifact.json").read_text())
    )
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        p0_rows = conn.execute(
            "SELECT p.* FROM posterior_checkpoints p JOIN ("
            "SELECT target_identity,MIN(rowid) AS first_rowid FROM posterior_checkpoints "
            "WHERE checkpoint_kind='initial' GROUP BY target_identity"
            ") first ON first.first_rowid=p.rowid ORDER BY p.target_identity"
        ).fetchall()
        inputs = []
        for p0_record in p0_rows:
            p0 = dict(p0_record)
            events = [
                _canonical_row(row)
                for row in conn.execute(
                    "SELECT * FROM canonical_observation_events WHERE target_identity=? "
                    "AND state IN ('accepted_primary','accepted_substitute') "
                    "AND first_actionable_seen_at>=? ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id",
                    (_text(p0.get("target_identity")), _text(p0.get("created_at"))),
                ).fetchall()
            ]
            if events:
                inputs.append((p0, events))
    first = [
        _replay_target(
            activation=activation,
            transition=transition,
            observation=observation,
            source_p0=p0,
            events=events,
        )
        for p0, events in inputs
    ]
    second = [
        _replay_target(
            activation=activation,
            transition=transition,
            observation=observation,
            source_p0=p0,
            events=events,
        )
        for p0, events in inputs
    ]
    deterministic = [item["checkpoint_hashes"] for item in first] == [item["checkpoint_hashes"] for item in second]
    event_count = sum(int(item["event_count"]) for item in first)
    gates = {
        "at_least_two_station_days": len(first) >= 2,
        "at_least_twenty_canonical_events": event_count >= 20,
        "deterministic_replay": deterministic,
        "all_hard_bounds_monotone": all(item["hard_bounds_monotone"] for item in first),
        "runtime_manifest_bound": activation["runtime_manifest_sha256"] == v6_runtime_manifest_sha256(root),
        "transition_artifact_bound": activation["transition_artifact_sha256"] == transition["artifact_sha256"],
        "observation_artifact_bound": activation["observation_artifact_sha256"] == observation["artifact_sha256"],
    }
    core = {
        "schema": SCHEMA_VERSION,
        "ok": all(gates.values()),
        "state": "canonical_production_replay_accepted" if all(gates.values()) else "rejected",
        "mode": "offline_production_cutover_replay_not_live_shadow",
        "activation_contract_sha256": activation["activation_contract_sha256"],
        "runtime_manifest_sha256": activation["runtime_manifest_sha256"],
        "transition_artifact_sha256": transition["artifact_sha256"],
        "observation_artifact_sha256": observation["artifact_sha256"],
        "target_count": len(first),
        "canonical_event_count": event_count,
        "targets": first,
        "gates": gates,
        "writes_archive_db": False,
        "writes_public_ssot": False,
        "places_orders": False,
    }
    return {**core, "replay_receipt_sha256": _sha(core)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--output", default=str(ROOT / "output/audits/physical_state_bayes_v6_canonical_replay.json"))
    args = parser.parse_args()
    result = audit(ROOT, Path(args.db).resolve())
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
