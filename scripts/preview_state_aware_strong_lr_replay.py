#!/usr/bin/env python3
"""Read-only full-day replay of the proposed state-aware Strong-LR epoch."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_activation_contract import (  # noqa: E402
    BAYES_FORMULA_ID,
    CHECKPOINT_SCHEMA,
    COMPOSER_SCHEMA,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION as ACTIVATION_SCHEMA,
    STRONG_UPDATE_SCHEMA,
    validate_production_probability_activation,
)
from production_probability_p0_runtime import (  # noqa: E402
    build_production_p0_inputs,
    compile_production_p0_candidate,
)
from production_probability_runtime_manifest import (  # noqa: E402
    build_current_retirement_receipt,
    build_current_runtime_manifest,
)
from peak_timing_state_runtime import (  # noqa: E402
    build_peak_timing_state,
    validate_peak_timing_state_contract,
)
from sequential_bayes_filter_shadow_runtime import replay_canonical_sequence  # noqa: E402
from sequential_bayes_shadow_pipeline import load_canonical_observation_event  # noqa: E402
from temperature_trajectory_feature_runtime import (  # noqa: E402
    build_temperature_trajectory_feature_state,
)


SCHEMA_VERSION = "state_aware_strong_lr_replay_preview_v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _preview_peak_timing_state(
    *,
    events: list[dict[str, Any]],
    replay: dict[str, Any],
    initial_lineage: dict[str, Any],
    decision_as_of: str,
) -> dict[str, Any]:
    """Expose the exact production timing contract without changing p(X)."""

    updates = list(replay.get("updates") or [])
    if not events or not updates:
        return {
            "schema": "peak_timing_state_l3_v1",
            "ok": True,
            "state": "na",
            "na_reason": "peak_timing_replay_events_or_updates_missing",
            "entry_peak_at": "",
            "does_not_update_physical_x": True,
            "timing_failure_does_not_block_probability": True,
            "read_only": True,
        }
    trajectory = build_temperature_trajectory_feature_state(
        canonical_events=events,
        decision_as_of=decision_as_of,
        temperature_quantization_c=0.25,
        temporal_role="post_event",
    )
    raw = build_peak_timing_state(
        forecast_peak_timing_prior=dict(
            initial_lineage.get("forecast_peak_timing_prior") or {}
        ),
        strong_observation_update=dict(updates[-1] or {}),
        post_event_trajectory=trajectory,
        canonical_observation_event=dict(events[-1] or {}),
    )
    if raw.get("ok") is True:
        return dict(raw)
    return {
        "schema": "peak_timing_state_l3_v1",
        "ok": True,
        "state": "na",
        "na_reason": "peak_timing_integrity_unavailable",
        "canonical_observation_event_id": str(
            events[-1].get("canonical_observation_event_id") or ""
        ),
        "strong_observation_update_id": str(
            updates[-1].get("strong_observation_update_id") or ""
        ),
        "forecast_evidence_snapshot_id": str(
            updates[-1].get("forecast_evidence_snapshot_id") or ""
        ),
        "entry_peak_at": "",
        "source_block_reasons": list(raw.get("block_reasons") or []),
        "does_not_update_physical_x": True,
        "timing_failure_does_not_block_probability": True,
        "read_only": True,
    }


def build_preview(*, db: Path, event_date: str, decision_as_of: str) -> dict[str, Any]:
    inputs = build_production_p0_inputs(event_date=event_date, decision_as_of=decision_as_of)
    if inputs.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": inputs.get("block_reasons") or ["p0_inputs_blocked"], "read_only": True, "places_orders": False}
    artifacts = dict(inputs.get("runtime_artifacts") or {})
    runtime_manifest = build_current_runtime_manifest()
    retirement_receipt = build_current_retirement_receipt()
    activation = validate_production_probability_activation(
        {
            "schema": ACTIVATION_SCHEMA,
            "state": "production_enabled",
            "production_transaction_b_authorized": True,
            "unique_probability_mainline": True,
            "legacy_probability_writer_disabled": True,
            "no_order_validation_accepted": True,
            "cutover_approval_id": "read-only-state-aware-replay-preview",
            "activation_effective_at": decision_as_of,
            "prior_model_artifact_sha256": artifacts["prior_model_artifact_sha256"],
            "weak_likelihood_artifact_sha256": artifacts["weak_likelihood_artifact_sha256"],
            "strong_lr_artifact_sha256": artifacts["strong_lr_artifact_sha256"],
            "runtime_manifest_sha256": _sha(runtime_manifest),
            "legacy_writer_retirement_receipt_sha256": _sha(retirement_receipt),
            "bayes_formula_id": BAYES_FORMULA_ID,
            "composer_schema": COMPOSER_SCHEMA,
            "checkpoint_schema": CHECKPOINT_SCHEMA,
            "strong_update_schema": STRONG_UPDATE_SCHEMA,
            "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
            "places_orders": False,
            "authorizes_clob": False,
        }
    )
    compiled = compile_production_p0_candidate(activation=activation, inputs=inputs)
    if compiled.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": compiled.get("block_reasons") or ["p0_compile_blocked"], "read_only": True, "places_orders": False}
    target = str(inputs["target_identity"])
    events: list[dict[str, Any]] = []
    with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
        event_ids = [
            str(row[0])
            for row in conn.execute(
                """SELECT canonical_observation_event_id
                   FROM canonical_observation_events
                   WHERE target_identity=? AND state IN ('accepted_primary','accepted_substitute')
                   ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id""",
                (target,),
            ).fetchall()
        ]
        for event_id in event_ids:
            loaded = load_canonical_observation_event(conn, canonical_observation_event_id=event_id)
            if loaded.get("ok") is not True:
                return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": [f"canonical_event_unavailable:{event_id}"], "read_only": True, "places_orders": False}
            events.append(dict(loaded["canonical_observation_event"]))
    initial_checkpoint = dict((compiled["candidate"] or {}).get("checkpoint") or {})
    replay = replay_canonical_sequence(
        initial_checkpoint=initial_checkpoint,
        canonical_events=events,
        forecast_evidence_snapshot_id=artifacts["weak_evidence_snapshot_id"],
        prior_context_id=str((inputs.get("prior_context") or {}).get("prior_context_id") or ""),
        approved_lr_artifact=dict(inputs["strong_lr_artifact"]),
        decision_as_of=decision_as_of,
        deadline_at=decision_as_of,
        fact_snapshot_id_prefix=f"preview:{target}",
        resolution_contract_id=str((inputs.get("resolution_contract") or {}).get("resolution_contract_id") or ""),
        source_reliability_pre_state=dict(initial_checkpoint.get("source_reliability_state") or {}),
        temperature_quantization_c=0.25,
    )
    if replay.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": [str(replay.get("reason") or "replay_blocked")], "replay": replay, "read_only": True, "places_orders": False}
    checkpoint = dict(replay.get("checkpoint") or {})
    peak_timing_state = _preview_peak_timing_state(
        events=events,
        replay=dict(replay),
        initial_lineage=dict((compiled.get("candidate") or {}).get("initial_lineage") or {}),
        decision_as_of=decision_as_of,
    )
    latest_update = dict((replay.get("updates") or [{}])[-1] or {})
    peak_timing_validation, peak_timing_issues = validate_peak_timing_state_contract(
        peak_timing_state,
        expected_event_id=str((events[-1] if events else {}).get("canonical_observation_event_id") or ""),
        expected_update_id=str(latest_update.get("strong_observation_update_id") or ""),
    )
    state_timeline: list[dict[str, Any]] = []
    for event, update in zip(events, replay.get("updates") or []):
        state_mass: dict[str, float] = {}
        for cell in update.get("candidate_post_joint") or []:
            state = str(cell.get("state") or "")
            state_mass[state] = state_mass.get(state, 0.0) + float(cell.get("probability") or 0.0)
        ranked_states = sorted(state_mass.items(), key=lambda item: (-item[1], item[0]))
        raw_by_state: dict[str, list[float]] = {}
        for row in update.get("raw_log_score") or []:
            state = str(row.get("pre_state") or "")
            value = row.get("state_log_score_adjustment")
            if value is not None:
                raw_by_state.setdefault(state, []).append(float(value))
        mean_state_adjustment = {
            state: sum(values) / len(values)
            for state, values in raw_by_state.items()
            if values
        }
        observation = event.get("parsed_observation") or {}
        first_raw_row = next(
            (row for row in update.get("raw_log_score") or [] if isinstance(row, dict)),
            {},
        )
        first_cb_values = dict(first_raw_row.get("feature_values") or {})
        first_cb_audit = dict(first_raw_row.get("feature_audit") or {})
        observed_peak_at_event = first_cb_values.get("trajectory.observed_peak_including_current_c")
        observed_peak_cb_penalty = next(
            (
                (row.get("operational_physics_trace") or {}).get("first_cb_remaining_warming_penalty")
                for row in update.get("raw_log_score") or []
                if row.get("temperature_c") == observed_peak_at_event
            ),
            None,
        )
        state_timeline.append(
            {
                "valid_time": event.get("valid_time"),
                "temperature_c": observation.get("temperature_c"),
                "top_state": ranked_states[0][0] if ranked_states else "",
                "top_state_probability": ranked_states[0][1] if ranked_states else None,
                "mean_state_log_score_adjustment": dict(
                    sorted(mean_state_adjustment.items(), key=lambda item: (-item[1], item[0]))
                ),
                "first_cb_event_state": first_cb_values.get("physical.first_cb_event_state"),
                "first_cb_timing_phase": first_cb_values.get("physical.first_cb_timing_phase"),
                "first_cb_peak_relation": first_cb_values.get("physical.first_cb_peak_relation"),
                "first_cb_valid_time": first_cb_audit.get("physical.first_cb_valid_time"),
                "first_cb_temperature_c": first_cb_audit.get("physical.first_cb_temperature_c"),
                "first_cb_penalty_at_observed_peak": observed_peak_cb_penalty,
            }
        )
    physical = dict(checkpoint.get("physical_high_distribution") or {})
    latent = dict(checkpoint.get("latent_post_state_distribution") or {})
    top_x = sorted(physical.items(), key=lambda item: (-float(item[1]), float(item[0])))[:8]
    top_states = sorted(latent.items(), key=lambda item: (-float(item[1]), item[0]))
    state_values = {round(float(value), 12) for value in latent.values()}
    observed_temperatures = [
        float((event.get("parsed_observation") or {}).get("temperature_c"))
        for event in events
        if (event.get("parsed_observation") or {}).get("temperature_c") is not None
    ]
    latest_event = events[-1] if events else {}
    latest_observation = latest_event.get("parsed_observation") or {}
    latest_temperature = (
        float(latest_observation.get("temperature_c"))
        if latest_observation.get("temperature_c") is not None
        else None
    )
    latest_valid_time = str(latest_event.get("valid_time") or "")
    try:
        latest_local_hour = datetime.fromisoformat(latest_valid_time.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).hour
    except ValueError:
        latest_local_hour = None
    observed_peak = max(observed_temperatures) if observed_temperatures else None
    late_cooling_case = bool(
        latest_local_hour is not None
        and latest_local_hour >= 17
        and latest_temperature is not None
        and observed_peak is not None
        and latest_temperature <= observed_peak - 1.0
    )
    late_cooling_supported = bool(
        not late_cooling_case
        or (
            top_states
            and top_states[0][0]
            in {"temporary_cooling", "terminal_cooling", "final_locked", "weather_suppressed"}
            and float(latent.get("warming") or 0.0) < 0.20
        )
    )
    first_cb_rows = [row for row in state_timeline if row.get("first_cb_event_state") == "first_cb_this_event"]
    first_cb_timing_supported = bool(
        not first_cb_rows
        or all(
            row.get("first_cb_valid_time")
            and row.get("first_cb_timing_phase") in {
                "early_before_peak_window", "peak_window", "late_after_peak_window"
            }
            and abs(float(row.get("first_cb_penalty_at_observed_peak") or 0.0)) <= 1e-12
            for row in first_cb_rows
        )
    )
    ok = bool(
        events
        and len(state_values) > 1
        and late_cooling_supported
        and first_cb_timing_supported
        and not peak_timing_issues
    )
    block_reasons = []
    if not events or len(state_values) <= 1:
        block_reasons.append("state_marginal_not_discriminating_or_events_missing")
    if not late_cooling_supported:
        block_reasons.append("late_cooling_directional_sanity_failed")
    if not first_cb_timing_supported:
        block_reasons.append("first_cb_temporal_causality_sanity_failed")
    block_reasons.extend(peak_timing_issues)
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "state": "replay_ready" if ok else "blocked",
        "block_reasons": block_reasons,
        "event_date": event_date,
        "target_identity": target,
        "canonical_event_count": len(events),
        "strong_lr_artifact_sha256": artifacts["strong_lr_artifact_sha256"],
        "top_physical_x": top_x,
        "peak_timing_state": peak_timing_state,
        "peak_timing_validation": {
            **peak_timing_validation,
            "passed": not peak_timing_issues,
            "issues": peak_timing_issues,
        },
        "latent_state_marginal": top_states,
        "latent_state_discriminating": len(state_values) > 1,
        "state_timeline_tail": state_timeline[-8:],
        "observed_max_lower_bound_c": checkpoint.get("observed_max_lower_bound_c"),
        "directional_sanity": {
            "late_cooling_case": late_cooling_case,
            "latest_local_hour": latest_local_hour,
            "latest_temperature_c": latest_temperature,
            "observed_peak_c": observed_peak,
            "warming_probability": float(latent.get("warming") or 0.0),
            "passed": late_cooling_supported,
        },
        "first_cb_sanity": {
            "first_cb_event_count": len(first_cb_rows),
            "first_cb_events": first_cb_rows,
            "observed_peak_never_penalized_by_first_cb": first_cb_timing_supported,
            "tcu_is_not_promoted_to_cb": True,
            "passed": first_cb_timing_supported,
        },
        "consumed_event_count": len(checkpoint.get("consumed_event_ids") or []),
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--decision-as-of", default="")
    args = parser.parse_args()
    report = build_preview(db=args.db, event_date=str(args.event_date)[:10], decision_as_of=str(args.decision_as_of or _now()))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
