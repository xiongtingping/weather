#!/usr/bin/env python3
"""Read-only successor replay for latent scheduled-slot Z -> reported AMSC X.

This preview changes no production artifact or pointer.  It replays the
approved prior/weak/strong evidence while removing SPECI from the hard-bound
operator, then projects every continuous latent checkpoint onto the exact
whole-degree regular-METAR target X.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from production_probability_activation_contract import load_production_probability_activation  # noqa: E402
from production_probability_p0_runtime import (  # noqa: E402
    build_production_p0_inputs,
    compile_production_p0_candidate,
)
import sequential_bayes_filter_shadow_runtime as filter_runtime  # noqa: E402
from sequential_bayes_shadow_pipeline import load_canonical_observation_event  # noqa: E402
from report_defined_physical_x_runtime import (  # noqa: E402
    advance_reported_x_lower_bound,
    build_report_defined_checkpoint_projection,
    build_reported_x_termination_marginal,
    project_joint_latent_high_to_reported_x,
)


SCHEMA_VERSION = "report_defined_x_successor_replay_preview_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _speci_soft_only_latent_bound(event: Mapping[str, Any]) -> float | None:
    """Retain METAR interval physics while forbidding a SPECI hard X bound."""

    if _text(event.get("report_type")).upper() == "SPECI":
        return None
    parsed = event.get("parsed_observation")
    parsed = parsed if isinstance(parsed, Mapping) else {}
    for value in (
        event.get("observed_max_lower_bound_c"),
        parsed.get("temperature_interval_lower_c"),
        parsed.get("observed_max_lower_bound_c"),
    ):
        try:
            if value is not None and _text(value):
                return float(value)
        except (TypeError, ValueError):
            pass
    try:
        return float(parsed.get("temperature_c")) - 0.5
    except (TypeError, ValueError):
        return None


def _load_events(conn: sqlite3.Connection, *, target_identity: str) -> list[dict[str, Any]]:
    ids = [
        _text(row[0])
        for row in conn.execute(
            """SELECT canonical_observation_event_id
               FROM canonical_observation_events
               WHERE target_identity=? AND state IN ('accepted_primary','accepted_substitute')
               ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id""",
            (target_identity,),
        ).fetchall()
    ]
    events: list[dict[str, Any]] = []
    for event_id in ids:
        loaded = load_canonical_observation_event(
            conn, canonical_observation_event_id=event_id
        )
        if loaded.get("ok") is not True:
            raise ValueError(f"canonical_event_unavailable:{event_id}")
        events.append(dict(loaded["canonical_observation_event"]))
    return events


def build_preview(*, db: Path, event_date: str, decision_as_of: str) -> dict[str, Any]:
    activation = load_production_probability_activation()
    inputs = build_production_p0_inputs(
        event_date=event_date,
        decision_as_of=decision_as_of,
    )
    if inputs.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(inputs.get("block_reasons") or ["p0_inputs_blocked"]),
            "read_only": True,
            "places_orders": False,
        }
    compiled = compile_production_p0_candidate(activation=activation, inputs=inputs)
    if compiled.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(compiled.get("block_reasons") or ["p0_compile_blocked"]),
            "read_only": True,
            "places_orders": False,
        }
    target = _text(inputs.get("target_identity"))
    with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
        events = _load_events(conn, target_identity=target)
    initial_checkpoint = dict((compiled.get("candidate") or {}).get("checkpoint") or {})
    artifacts = dict(inputs.get("runtime_artifacts") or {})
    original_lower_bound = filter_runtime._event_lower_bound
    filter_runtime._event_lower_bound = _speci_soft_only_latent_bound
    try:
        replay = filter_runtime.replay_canonical_sequence(
            initial_checkpoint=initial_checkpoint,
            canonical_events=events,
            forecast_evidence_snapshot_id=_text(artifacts.get("weak_evidence_snapshot_id")),
            prior_context_id=_text((inputs.get("prior_context") or {}).get("prior_context_id")),
            approved_lr_artifact=dict(inputs.get("strong_lr_artifact") or {}),
            decision_as_of=decision_as_of,
            deadline_at=decision_as_of,
            fact_snapshot_id_prefix=f"report-x-preview:{target}",
            resolution_contract_id=_text((inputs.get("resolution_contract") or {}).get("resolution_contract_id")),
            source_reliability_pre_state=dict(initial_checkpoint.get("source_reliability_state") or {}),
            temperature_quantization_c=0.25,
        )
    finally:
        filter_runtime._event_lower_bound = original_lower_bound
    if replay.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": [_text(replay.get("reason")) or "successor_replay_blocked"],
            "replay": replay,
            "read_only": True,
            "places_orders": False,
        }

    timeline: list[dict[str, Any]] = []
    reported_bound: int | None = None
    for event, update in zip(events, replay.get("updates") or []):
        bound_update = advance_reported_x_lower_bound(reported_bound, event)
        if bound_update.get("ok") is not True:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "block_reasons": list(bound_update.get("block_reasons") or ["reported_x_bound_blocked"]),
                "read_only": True,
                "places_orders": False,
            }
        reported_bound = bound_update.get("reported_x_lower_bound_c")
        projection = project_joint_latent_high_to_reported_x(
            update.get("candidate_post_joint") or [],
            reported_x_lower_bound_c=reported_bound,
        )
        if projection.get("ok") is not True:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "block_reasons": list(projection.get("block_reasons") or ["reported_x_projection_blocked"]),
                "read_only": True,
                "places_orders": False,
            }
        distribution = dict(projection.get("reported_x_distribution") or {})
        termination = build_reported_x_termination_marginal(
            distribution,
            reported_x_lower_bound_c=reported_bound,
        )
        timeline.append(
            {
                "canonical_observation_event_id": _text(event.get("canonical_observation_event_id")),
                "valid_time": _text(event.get("valid_time")),
                "report_type": _text(event.get("report_type")),
                "reported_temperature_c": (event.get("parsed_observation") or {}).get("temperature_c"),
                "reported_x_lower_bound_c": reported_bound,
                "speci_changed_reported_x_bound": (
                    _text(event.get("report_type")).upper() == "SPECI"
                    and len(timeline) > 0
                    and reported_bound != timeline[-1].get("reported_x_lower_bound_c")
                ),
                "top_reported_x": sorted(
                    distribution.items(),
                    key=lambda item: (-float(item[1]), float(item[0])),
                )[:5],
                "reported_x_termination": termination,
            }
        )
    checkpoint_projection = build_report_defined_checkpoint_projection(
        replay.get("checkpoint") or {},
        canonical_events=events,
    )
    if checkpoint_projection.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(
                checkpoint_projection.get("block_reasons")
                or ["successor_checkpoint_projection_blocked"]
            ),
            "checkpoint_projection": checkpoint_projection,
            "read_only": True,
            "places_orders": False,
        }
    final_distribution = dict(
        checkpoint_projection.get("reported_physical_x_distribution") or {}
    )
    integer_support = all(float(value).is_integer() for value in final_distribution)
    no_mass_below_bound = bool(
        reported_bound is None
        or all(float(value) >= reported_bound for value in final_distribution)
    )
    speci_bound_stable = not any(
        row.get("speci_changed_reported_x_bound") for row in timeline
    )
    ok = bool(events and integer_support and no_mass_below_bound and speci_bound_stable)
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "state": "successor_replay_ready" if ok else "blocked",
        "block_reasons": [
            reason
            for condition, reason in (
                (not events, "canonical_events_missing"),
                (not integer_support, "reported_x_support_not_integer"),
                (not no_mass_below_bound, "reported_x_mass_below_regular_metar_max"),
                (not speci_bound_stable, "speci_changed_reported_x_hard_bound"),
            )
            if condition
        ],
        "event_date": event_date,
        "target_identity": target,
        "canonical_event_count": len(events),
        "reported_x_lower_bound_c": reported_bound,
        "reported_x_distribution": final_distribution,
        "top_reported_x": sorted(
            final_distribution.items(),
            key=lambda item: (-float(item[1]), float(item[0])),
        )[:8],
        "latent_scheduled_slot_high_distribution": dict(
            checkpoint_projection.get("latent_scheduled_slot_high_distribution") or {}
        ),
        "reported_x_termination": dict(
            checkpoint_projection.get("reported_x_termination") or {}
        ),
        "checkpoint_projection_sha256": _text(
            checkpoint_projection.get("projection_sha256")
        ),
        "timeline": timeline,
        "invariants": {
            "continuous_internal_state_is_not_public_x": True,
            "reported_x_support_is_integer": integer_support,
            "regular_metar_exact_max_is_hard_x_bound": no_mass_below_bound,
            "speci_is_soft_strong_evidence_not_x_hard_bound": speci_bound_stable,
            "termination_is_same_posterior_marginal_not_second_update": True,
        },
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--decision-as-of", default="")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = build_preview(
        db=args.db,
        event_date=_text(args.event_date)[:10],
        decision_as_of=_text(args.decision_as_of) or _now(),
    )
    if args.summary and result.get("ok") is True:
        result = {
            key: result[key]
            for key in (
                "schema",
                "ok",
                "state",
                "event_date",
                "target_identity",
                "canonical_event_count",
                "reported_x_lower_bound_c",
                "top_reported_x",
                "reported_x_termination",
                "invariants",
                "read_only",
                "writes_archive_db",
                "places_orders",
            )
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
