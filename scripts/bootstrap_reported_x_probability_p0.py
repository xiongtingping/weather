#!/usr/bin/env python3
"""Commit reported-X P0 and replay all accepted canonical observations."""

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

from archive_db_write_queue import connect_archive_db_rw  # noqa: E402
from lineage_identity_runtime import build_correlation_id  # noqa: E402
from production_probability_p0_runtime import (  # noqa: E402
    build_production_p0_inputs,
    build_zuuu_physical_public_slug,
)
from production_probability_successor_activation_contract import (  # noqa: E402
    DEFAULT_PATH as DEFAULT_ACTIVATION,
    load_successor_activation,
)
from production_reported_x_canonical_event_drain_runtime import (  # noqa: E402
    drain_reported_x_canonical_events,
)
from production_reported_x_initial_checkpoint import (  # noqa: E402
    compile_reported_x_initial_checkpoint,
    rebase_reported_x_initial_successor,
)
from production_reported_x_initial_writer import commit_reported_x_initial_posterior  # noqa: E402


SCHEMA_VERSION = "bootstrap_reported_x_probability_p0_v1"
CONFIRMATION = "BOOTSTRAP_REPORTED_X_PROBABILITY_P0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _compile(activation: dict, inputs: dict) -> dict:
    weak = dict((inputs.get("weak_likelihood") or {}).get("joint_weak_likelihood") or {})
    return compile_reported_x_initial_checkpoint(
        activation=activation,
        target_identity=str(inputs.get("target_identity") or ""),
        event_date=str(inputs.get("event_date") or ""),
        station_id="ZUUU",
        decision_as_of=str(inputs.get("decision_as_of") or ""),
        prior_context=dict(inputs.get("prior_context") or {}),
        joint_weak_likelihood=weak,
        active_strong_lr_artifact=dict(inputs.get("strong_lr_artifact") or {}),
        resolution_contract=dict(inputs.get("resolution_contract") or {}),
        source_reliability_initial_state={
            "amsc_zuuu_scheduled_metar_daily_max_v2": {"state": "unknown"},
            "amsc_zuuu_speci_realtime_v1": {"state": "unknown"},
            "awc_zuuu_metar_crosscheck": {"state": "unknown"},
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--activation", default=str(DEFAULT_ACTIVATION))
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--decision-as-of", default="")
    parser.add_argument("--public-slug", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    day = str(args.event_date)[:10]
    as_of = str(args.decision_as_of or _now())
    inputs = build_production_p0_inputs(event_date=day, decision_as_of=as_of)
    result = {
        "schema": SCHEMA_VERSION,
        "ok": inputs.get("ok") is True,
        "state": "ready" if inputs.get("ok") is True else "blocked",
        "event_date": day,
        "block_reasons": list(inputs.get("block_reasons") or []),
        "places_orders": False,
    }
    if not args.apply:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.confirm != CONFIRMATION or inputs.get("ok") is not True:
        result.update({"ok": False, "state": "apply_blocked"})
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        activation = load_successor_activation(args.activation)
        candidate = _compile(activation, inputs)
        if candidate.get("ok") is not True:
            raise ValueError("reported_x_p0_candidate_blocked")
        slug = str(args.public_slug or build_zuuu_physical_public_slug(day))
        with connect_archive_db_rw(Path(args.db), owner="reported_x_probability_p0") as conn:
            existing = conn.execute(
                """SELECT posterior_checkpoint_id,replay_generation FROM posterior_checkpoints
                   WHERE target_identity=? AND state='committed'
                   ORDER BY replay_generation DESC,rowid DESC""",
                (str(inputs.get("target_identity") or ""),),
            ).fetchall()
            if existing:
                candidate = rebase_reported_x_initial_successor(
                    candidate,
                    replay_generation=max(int(row[1] or 0) for row in existing) + 1,
                    supersedes_checkpoint_id=str(existing[0][0] or ""),
                )
            committed = commit_reported_x_initial_posterior(
                conn,
                activation=activation,
                initial_checkpoint=candidate,
                slug=slug,
                event_date=day,
                fact_as_of=as_of,
                correlation_id=build_correlation_id("reported_x_p0", day, as_of),
            )
        with connect_archive_db_rw(Path(args.db), owner="reported_x_probability_replay") as conn:
            replay = drain_reported_x_canonical_events(
                conn, activation=activation, event_date=day, public_slug=slug
            )
        result.update(
            {
                "ok": committed.get("ok") is True and replay.get("ok") is True,
                "state": "p0_committed_and_canonical_replayed" if replay.get("ok") is True else "p0_committed_replay_blocked",
                "p0_commit": committed,
                "canonical_replay": replay,
            }
        )
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        result.update({"ok": False, "state": "blocked", "block_reasons": [f"bootstrap_failed:{type(exc).__name__}:{exc}"]})
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
