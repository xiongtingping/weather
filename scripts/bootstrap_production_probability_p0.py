#!/usr/bin/env python3
"""Build, commit and replay the current production sequential-Bayes P0."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive_db_write_queue import connect_archive_db_rw  # noqa: E402
from lineage_identity_runtime import build_correlation_id  # noqa: E402
from production_canonical_event_drain_runtime import drain_production_canonical_events  # noqa: E402
from production_initial_posterior_writer import commit_production_initial_posterior  # noqa: E402
from production_initial_posterior_checkpoint import rebase_production_initial_posterior_successor  # noqa: E402
from production_probability_activation_contract import (  # noqa: E402
    DEFAULT_PATH as DEFAULT_ACTIVATION,
    load_production_probability_activation,
)
from production_probability_p0_runtime import (  # noqa: E402
    DEFAULT_NCEI_ROOT,
    DEFAULT_RESOLUTION_AUTHORITY,
    build_production_p0_inputs,
    build_zuuu_physical_public_slug,
    compile_production_p0_candidate,
)


SCHEMA_VERSION = "bootstrap_production_probability_p0_v1"
CONFIRMATION = "BOOTSTRAP_PRODUCTION_PROBABILITY_P0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _summary(inputs: dict, compiled: dict | None = None) -> dict:
    weak = ((inputs.get("weak_likelihood") or {}).get("joint_weak_likelihood") or {})
    return {
        "schema": SCHEMA_VERSION,
        "ok": inputs.get("ok") is True and (compiled is None or compiled.get("ok") is True),
        "state": compiled.get("state") if compiled is not None else inputs.get("state"),
        "event_date": inputs.get("event_date"),
        "decision_as_of": inputs.get("decision_as_of"),
        "target_identity": inputs.get("target_identity"),
        "runtime_artifacts": inputs.get("runtime_artifacts"),
        "weak_source_set": weak.get("weak_source_set"),
        "prior_context_id": (inputs.get("prior_context") or {}).get("prior_context_id"),
        "block_reasons": inputs.get("block_reasons") or (compiled or {}).get("block_reasons") or [],
        "places_orders": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--activation", default=str(DEFAULT_ACTIVATION))
    parser.add_argument("--event-date", default=_today())
    parser.add_argument("--decision-as-of", default="")
    parser.add_argument("--ncei-root", default=str(DEFAULT_NCEI_ROOT))
    parser.add_argument("--resolution-authority", default=str(DEFAULT_RESOLUTION_AUTHORITY))
    parser.add_argument("--public-slug", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    event_date = str(args.event_date or "")[:10]
    as_of = str(args.decision_as_of or _now())
    inputs = build_production_p0_inputs(
        event_date=event_date,
        decision_as_of=as_of,
        ncei_root=args.ncei_root,
        resolution_authority_path=args.resolution_authority,
    )
    if not args.apply:
        result = _summary(inputs)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") is True else 1
    if args.confirm != CONFIRMATION or event_date != _today():
        result = {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["confirmation_required_or_noncurrent_event_date"], "places_orders": False}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        activation = load_production_probability_activation(args.activation)
        compiled = compile_production_p0_candidate(activation=activation, inputs=inputs)
        if compiled.get("ok") is not True:
            result = _summary(inputs, compiled)
        else:
            slug = str(args.public_slug or "").strip()
            if not slug:
                slug = build_zuuu_physical_public_slug(event_date)
            with connect_archive_db_rw(Path(args.db), owner="production_probability_p0") as conn:
                target_identity = str(inputs.get("target_identity") or "")
                existing = conn.execute(
                    """SELECT posterior_checkpoint_id,replay_generation
                       FROM posterior_checkpoints
                       WHERE target_identity=? AND state='committed'
                       ORDER BY replay_generation DESC,rowid DESC""",
                    (target_identity,),
                ).fetchall()
                if existing:
                    compiled["candidate"] = rebase_production_initial_posterior_successor(
                        compiled["candidate"],
                        replay_generation=max(int(row[1] or 0) for row in existing) + 1,
                        supersedes_checkpoint_id=str(existing[0][0] or ""),
                    )
                committed = commit_production_initial_posterior(
                    conn,
                    activation=activation,
                    initial_checkpoint=compiled["candidate"],
                    slug=slug,
                    event_date=event_date,
                    fact_as_of=as_of,
                    correlation_id=build_correlation_id("production_p0", event_date, as_of),
                )
            with connect_archive_db_rw(Path(args.db), owner="production_probability_p0_replay") as conn:
                replay = drain_production_canonical_events(
                    conn,
                    activation=activation,
                    event_date=event_date,
                    public_slug=slug,
                    resolution_authority_path=args.resolution_authority,
                )
            result = {
                **_summary(inputs, compiled),
                "state": "p0_committed_and_canonical_replayed" if replay.get("ok") is True else "p0_committed_replay_blocked",
                "p0_commit": committed,
                "canonical_replay": replay,
                "ok": committed.get("ok") is True and replay.get("ok") is True,
            }
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        result = {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": [f"bootstrap_failed:{type(exc).__name__}:{exc}"], "places_orders": False}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
