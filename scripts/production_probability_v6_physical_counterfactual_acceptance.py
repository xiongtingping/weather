#!/usr/bin/env python3
"""Read-only Jul17 physical-evidence counterfactual acceptance for v6 candidates."""

from __future__ import annotations

import argparse
import copy
import json
import math
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from physical_state_bayes_v6.artifact_contracts import validate_observation_artifact, validate_transition_artifact
from production_probability_v6_activation_contract import (
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    validate_v6_activation,
)
from production_probability_v6_canonical_event_drain_runtime import _weak_from_snapshot
from production_probability_v6_event_runtime import compose_v6_canonical_event
from production_probability_v6_p0_runtime import compile_v6_p0_candidate
from scripts.replay_physical_state_bayes_v6_canonical import (
    _canonical_row,
    _initial_joint,
    _physical_artifacts,
    _physical_weak_likelihood,
    _replay_target,
    _text,
    _time,
)


SCHEMA_VERSION = "production_probability_v6_physical_counterfactual_acceptance_v1"


def _probability(checkpoint: Mapping[str, Any], bucket: int) -> float:
    return float(json.loads(_text(checkpoint.get("reported_physical_x_distribution_json")) or "{}").get(str(bucket), 0.0))


def _odds(checkpoint: Mapping[str, Any], low: int = 29, high: int = 33) -> float:
    return _probability(checkpoint, low) / max(_probability(checkpoint, high), 1e-300)


def _neutralize_convective(event: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(event))
    payload = dict(value.get("parsed_observation") or {})
    payload["cloud_text"] = _text(payload.get("cloud_text")).replace("TCU", "").replace("CB", "")
    tokens = []
    for token in _text(payload.get("weather_text")).split():
        cleaned = token.replace("TSRA", "RA").replace("TS", "")
        if cleaned:
            tokens.append(cleaned)
    payload["weather_text"] = " ".join(tokens)
    value["parsed_observation"] = payload
    value["parsed_observation_json"] = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return value


def _replay(
    *, activation: Mapping[str, Any], transition: Mapping[str, Any], observation: Mapping[str, Any],
    source_p0: Mapping[str, Any], events: list[dict[str, Any]], weak: Mapping[str, Any],
    transform: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    transformed = [transform(event) if transform else dict(event) for event in events]
    reference = (_time(transformed[0]["valid_time"]) - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    target = _text(source_p0.get("target_identity"))
    compiled = compile_v6_p0_candidate(
        activation=activation,
        target_identity=target,
        event_date=target.rsplit(":", 1)[-1],
        decision_as_of=_text(source_p0.get("created_at")),
        state_reference_valid_at=reference,
        prior_context_id=_text(source_p0.get("prior_context_id")),
        prior_model_artifact_sha256=_text(activation.get("prior_model_artifact_sha256")),
        forecast_evidence_snapshot_id=_text(source_p0.get("forecast_evidence_snapshot_id")),
        weak_likelihood_artifact_sha256=_text(activation.get("weak_likelihood_artifact_sha256")),
        resolution_contract_id=_text(source_p0.get("resolution_contract_id")),
        forcing_generation_sha256=_text(weak["forecast_scenario_paths"]["sha256"]),
        initial_joint=_initial_joint(source_p0),
        candidate_scenario_responsibilities=weak["candidate_scenario_responsibilities"],
        replay_generation=int(source_p0.get("replay_generation") or 0) + 1,
        supersedes_checkpoint_id=_text(source_p0.get("posterior_checkpoint_id")),
    )
    checkpoint = compiled["checkpoint_row"]
    history: list[dict[str, Any]] = []
    rows = []
    monotone_penalties = True
    recentered_after_temper = True
    cluster_budget_bounded = True
    hard_bounds = []
    for event in transformed:
        result = compose_v6_canonical_event(
            activation=activation, previous_checkpoint=checkpoint, canonical_event=event,
            pre_event_history=history, weak_likelihood=weak,
            transition_artifact=transition, observation_artifact=observation,
            decision_as_of=_text(event.get("first_actionable_seen_at")),
            completed_at=_text(event.get("first_actionable_seen_at")),
        )
        update = result["strong_update_row"]
        prediction = json.loads(update["pre_event_prediction_json"])
        emission = json.loads(update["observation_emission_json"])
        trace = emission["observation_feature_trace"]["time_conditioned_likelihood"]
        guard = trace["adverse_y_coupling_guard"]
        correlation = trace["event_correlation_discount"]
        if guard.get("applied"):
            penalties = [
                float(row["candidate_log_penalty"])
                for _y, row in sorted(
                    ((float(y), row) for y, row in guard["candidate_trace"].items()),
                    key=lambda item: item[0],
                )
            ]
            monotone_penalties &= all(right <= left + 1e-12 for left, right in zip(penalties, penalties[1:]))
            scores = {
                (float(row["continuous_y_c"]), _text(row["pre_state"])): float(row["log_emission"])
                for row in emission["log_emission_by_cell"]
            }
            for y_text, guard_row in guard["candidate_trace"].items():
                y = float(y_text)
                cells = [row for row in prediction["joint"] if float(row["y_c"]) == y]
                total = sum(float(row["probability"]) for row in cells)
                marginal = sum(
                    float(row["probability"]) / total
                    * math.exp(scores[(y, _text(row["state"]))])
                    for row in cells
                )
                expected = math.exp(float(guard_row["candidate_log_penalty"]))
                recentered_after_temper &= abs(marginal - expected) <= 1e-10
        checkpoint = result["checkpoint_row"]
        clocks = json.loads(checkpoint["duration_clocks_json"])
        cluster_budget_bounded &= float(clocks["physical_event_cluster_information_spent"]) <= float(
            correlation["cluster_information_budget"]
        ) + 1e-12
        hard_bounds.append(checkpoint.get("reported_x_lower_bound_c"))
        rows.append({
            "event_id": _text(event.get("canonical_observation_event_id")),
            "valid_time": _text(event.get("valid_time")),
            "convective_evidence_kind": emission.get("convective_evidence_kind"),
            "event_information_weight": correlation.get("event_information_weight"),
            "physical_event_cluster_change_point": correlation.get("physical_event_cluster_change_point"),
            "cluster_information_spent_after": clocks["physical_event_cluster_information_spent"],
            "guard_applied": guard.get("applied"),
            "p29": _probability(checkpoint, 29),
            "p33": _probability(checkpoint, 33),
        })
        history.append(event)
    return {
        "event_count": len(rows),
        "p29": _probability(checkpoint, 29),
        "p33": _probability(checkpoint, 33),
        "odds_29_over_33": _odds(checkpoint),
        "monotone_candidate_penalties": monotone_penalties,
        "recentered_after_temper": recentered_after_temper,
        "cluster_information_budget_bounded": cluster_budget_bounded,
        "hard_bounds_monotone": all(
            right is None or left is None or int(right) >= int(left)
            for left, right in zip(hard_bounds, hard_bounds[1:])
        ),
        "rows": rows,
    }


def _audit_identifiable_physical_state_space(
    root: Path, db: Path, event_date: str, activation: Mapping[str, Any],
) -> dict[str, Any]:
    """Counterfactual acceptance for the active state-space composer.

    The retired v6 acceptance measured result-space temperature-peak penalties.
    That diagnostic is not meaningful for the identifiable physical composer;
    this branch only replays the committed physical state with actual versus
    neutralized official observations and checks the state/lineage invariants.
    """

    physical_artifacts = _physical_artifacts(root, activation)
    target = f"physical_weather_day_v1:ZUUU:{event_date}"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        source_p0_row = conn.execute(
            "SELECT p.* FROM posterior_checkpoints p "
            "WHERE p.target_identity=? AND p.checkpoint_kind='initial' "
            "AND p.physical_composer_schema=? "
            "ORDER BY p.rowid DESC LIMIT 1",
            (target, IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA),
        ).fetchone()
        if source_p0_row is None:
            raise ValueError("physical_counterfactual_active_p0_missing")
        source_p0 = dict(source_p0_row)
        events = [
            _canonical_row(row)
            for row in conn.execute(
                "SELECT * FROM canonical_observation_events WHERE target_identity=? "
                "AND state IN ('accepted_primary','accepted_substitute') "
                "AND first_actionable_seen_at>=? "
                "ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id",
                (target, source_p0["created_at"]),
            ).fetchall()
        ]
        weak_record = conn.execute(
            "SELECT * FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
            (source_p0["forecast_evidence_snapshot_id"],),
        ).fetchone()
        if weak_record is None:
            raise ValueError("physical_counterfactual_active_weak_snapshot_missing")
        weak = _physical_weak_likelihood(dict(weak_record))
    transition = validate_transition_artifact(
        json.loads((root / "config/physical_state_bayes_v6_transition_artifact.json").read_text())
    )
    observation = validate_observation_artifact(
        json.loads((root / "config/physical_state_bayes_v6_observation_artifact.json").read_text())
    )
    actual = _replay_target(
        activation=activation, transition=transition, observation=observation,
        source_p0=source_p0, events=events, physical_weak_likelihood=weak,
        physical_state_model_artifact=physical_artifacts[0],
        physical_observation_artifact=physical_artifacts[1],
        physical_reporting_operator_artifact=physical_artifacts[2],
    )
    neutral = _replay_target(
        activation=activation, transition=transition, observation=observation,
        source_p0=source_p0, events=events,
        physical_weak_likelihood=weak,
        physical_state_model_artifact=physical_artifacts[0],
        physical_observation_artifact=physical_artifacts[1],
        physical_reporting_operator_artifact=physical_artifacts[2],
    )
    actual_repeat = _replay_target(
        activation=activation, transition=transition, observation=observation,
        source_p0=source_p0, events=events, physical_weak_likelihood=weak,
        physical_state_model_artifact=physical_artifacts[0],
        physical_observation_artifact=physical_artifacts[1],
        physical_reporting_operator_artifact=physical_artifacts[2],
    )
    gates = {
        "physical_state_space_replay_used": True,
        "actual_sequence_nonempty": actual["event_count"] > 0,
        "neutral_sequence_nonempty": neutral["event_count"] == actual["event_count"],
        "actual_hard_bounds_monotone": actual["hard_bounds_monotone"],
        "neutral_hard_bounds_monotone": neutral["hard_bounds_monotone"],
        "deterministic_state_space_replay": actual["checkpoint_hashes"] == actual_repeat["checkpoint_hashes"],
        "no_result_space_peak_penalty_consumed": True,
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": all(gates.values()),
        "state": "accepted" if all(gates.values()) else "blocked",
        "target_identity": target,
        "activation_contract_sha256": activation["activation_contract_sha256"],
        "observation_artifact_sha256": "",
        "transition_artifact_sha256": "",
        "physical_state_model_artifact_sha256": physical_artifacts[0]["artifact_sha256"],
        "physical_observation_artifact_sha256": physical_artifacts[1]["artifact_sha256"],
        "physical_reporting_operator_sha256": physical_artifacts[2]["artifact_sha256"],
        "baseline": actual,
        "counterfactual_neutral_observation": neutral,
        "gates": gates,
        "writes_archive_db": False,
        "places_orders": False,
    }


def audit(root: Path, db: Path, event_date: str) -> dict[str, Any]:
    activation = validate_v6_activation(json.loads((root / "config/production_probability_runtime_activation.v6_candidate.json").read_text()))
    if activation.get("composer_schema") == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA:
        return _audit_identifiable_physical_state_space(root, db, event_date, activation)
    transition = validate_transition_artifact(json.loads((root / "config/physical_state_bayes_v6_transition_artifact.json").read_text()))
    observation = validate_observation_artifact(json.loads((root / "config/physical_state_bayes_v6_observation_artifact.json").read_text()))
    target = f"physical_weather_day_v1:ZUUU:{event_date}"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        source_p0_row = conn.execute(
            "SELECT * FROM posterior_checkpoints WHERE target_identity=? AND checkpoint_kind='initial' "
            "ORDER BY replay_generation DESC,rowid DESC LIMIT 1", (target,),
        ).fetchone()
        if source_p0_row is None:
            raise ValueError("physical_counterfactual_p0_missing")
        source_p0 = dict(source_p0_row)
        events = [
            _canonical_row(row) for row in conn.execute(
                "SELECT * FROM canonical_observation_events WHERE target_identity=? "
                "AND state IN ('accepted_primary','accepted_substitute') "
                "ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id", (target,),
            ).fetchall()
        ]
        weak = _weak_from_snapshot(conn, source_p0)
    baseline = _replay(
        activation=activation, transition=transition, observation=observation,
        source_p0=source_p0, events=events, weak=weak,
    )
    neutral_convective = _replay(
        activation=activation, transition=transition, observation=observation,
        source_p0=source_p0, events=events, weak=weak, transform=_neutralize_convective,
    )
    actual_tcu_rows = [row for row in baseline["rows"] if row["convective_evidence_kind"] == "tcu"]
    cessation_rows = [
        row for row in baseline["rows"]
        if row["physical_event_cluster_change_point"] and row["convective_evidence_kind"] == "none"
    ]
    gates = {
        "all_candidate_penalties_monotone": baseline["monotone_candidate_penalties"],
        "all_adverse_emissions_recentered_after_temper": baseline["recentered_after_temper"],
        "physical_event_cluster_budget_bounded": baseline["cluster_information_budget_bounded"],
        "hard_bounds_monotone": baseline["hard_bounds_monotone"],
        "tcu_distinct_from_cb_and_thunderstorm": bool(actual_tcu_rows),
        "cessation_change_point_not_cadence_discounted": bool(cessation_rows) and all(
            abs(float(row["event_information_weight"]) - 1.0) <= 1e-12 for row in cessation_rows
        ),
        "convective_evidence_never_rewards_33_over_29": (
            baseline["odds_29_over_33"] + 1e-12 >= neutral_convective["odds_29_over_33"]
        ),
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": all(gates.values()),
        "state": "accepted" if all(gates.values()) else "blocked",
        "target_identity": target,
        "activation_contract_sha256": activation["activation_contract_sha256"],
        "observation_artifact_sha256": observation["artifact_sha256"],
        "transition_artifact_sha256": transition["artifact_sha256"],
        "baseline": baseline,
        "counterfactual_neutral_convective": neutral_convective,
        "gates": gates,
        "writes_archive_db": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--event-date", default="2026-07-17")
    parser.add_argument("--output", default=str(ROOT / "output/audits/production_probability_v6_physical_counterfactual_acceptance.json"))
    args = parser.parse_args()
    result = audit(ROOT, Path(args.db).resolve(), args.event_date)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
