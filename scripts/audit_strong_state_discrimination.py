#!/usr/bin/env python3
"""Read-only audit of latent-state discrimination on the latest checkpoint."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strong_observation_lr_operational_state_baseline import (  # noqa: E402
    SCORING_KIND,
    build_operational_state_aware_strong_lr_baseline,
)


def build_report(db_path: Path) -> dict:
    uri = f"file:{db_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        checkpoint = conn.execute("SELECT * FROM posterior_checkpoints ORDER BY rowid DESC LIMIT 1").fetchone()
        if checkpoint is None:
            return {"schema": "strong_state_discrimination_audit_v2", "ok": False, "state": "blocked", "block_reasons": ["posterior_checkpoint_missing"], "read_only": True}
        update = conn.execute(
            "SELECT * FROM strong_observation_updates WHERE strong_observation_update_id=?",
            (checkpoint["last_strong_update_id"],),
        ).fetchone()
    if update is None:
        return {"schema": "strong_state_discrimination_audit_v2", "ok": False, "state": "blocked", "block_reasons": ["strong_update_missing"], "read_only": True}
    raw_rows = json.loads(update["raw_log_score_json"])
    joint = json.loads(checkpoint["joint_posterior_json"])
    artifact = build_operational_state_aware_strong_lr_baseline()
    state_score_by_temperature: dict[float, list[float]] = defaultdict(list)
    for row in raw_rows:
        state_score_by_temperature[float(row["temperature_c"])].append(float(row["raw_log_score"]))
    state_score_discriminating = any(
        len({round(score, 12) for score in scores}) > 1
        for scores in state_score_by_temperature.values()
    )
    x_marginal: dict[float, float] = defaultdict(float)
    for row in joint:
        x_marginal[float(row["temperature_c"])] += float(row["probability"])
    production_state = json.loads(checkpoint["latent_post_state_json"])
    production_values = [round(float(value), 12) for value in production_state.values()]
    scoring_kinds = sorted({str(row.get("scoring_kind") or "") for row in raw_rows})
    row_artifact_matches = str(update["model_artifact_sha256"] or "") == artifact["artifact_sha256"]
    post_transition = json.loads(update["post_state_transition_json"] or "{}")
    transition_row_normalized = bool(post_transition) and all(
        isinstance(row, dict) and abs(sum(float(value) for value in row.values()) - 1.0) <= 1e-8
        for row in post_transition.values()
    )
    transition_dynamic = transition_row_normalized and any(
        row != {state: 1.0}
        for state, row in post_transition.items()
    )
    blockers: list[str] = []
    if scoring_kinds != [SCORING_KIND]:
        blockers.append("production_strong_lr_scoring_kind_not_state_aware_v2")
    if not row_artifact_matches:
        blockers.append("production_strong_lr_artifact_not_state_aware_v2")
    if not state_score_discriminating:
        blockers.append("production_raw_log_lr_not_state_discriminating")
    if not transition_row_normalized:
        blockers.append("production_post_state_transition_not_row_normalized")
    if not transition_dynamic:
        blockers.append("production_post_state_transition_still_identity")
    return {
        "schema": "strong_state_discrimination_audit_v2",
        "ok": not blockers,
        "state": "audited" if not blockers else "blocked",
        "block_reasons": blockers,
        "checkpoint_id": checkpoint["posterior_checkpoint_id"],
        "strong_update_id": update["strong_observation_update_id"],
        "decision_as_of": update["decision_as_of"],
        "production_strong_lr_artifact_sha256": str(update["model_artifact_sha256"] or ""),
        "expected_strong_lr_artifact_sha256": artifact["artifact_sha256"],
        "artifact_matches": row_artifact_matches,
        "scoring_kinds": scoring_kinds,
        "raw_log_lr_state_discriminating": state_score_discriminating,
        "post_state_transition_row_normalized": transition_row_normalized,
        "post_state_transition_dynamic": transition_dynamic,
        "production_state_marginal": dict(sorted(production_state.items(), key=lambda item: item[1], reverse=True)),
        "production_state_discriminating": len(set(production_values)) > 1,
        "production_top_physical_x": sorted(x_marginal.items(), key=lambda item: item[1], reverse=True)[:8],
        "boundary": {
            "writes_archive_db": False,
            "changes_activation": False,
            "places_orders": False,
            "audits_unique_production_evaluator": True,
        },
        "read_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    args = parser.parse_args()
    report = build_report(args.db)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
