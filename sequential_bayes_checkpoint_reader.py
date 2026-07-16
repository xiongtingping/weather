"""Read durable sequential-Bayes checkpoints into the inference contract."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


SCHEMA_VERSION = "sequential_bayes_checkpoint_reader_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _json_list(value: Any) -> list[Any] | None:
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError):
        return None
    return list(parsed) if isinstance(parsed, list) else None


def _json_object(value: Any) -> dict[str, Any] | None:
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def load_latest_persisted_checkpoint(
    conn: sqlite3.Connection,
    *,
    target_identity: str,
    event_date: str,
    replay_generation: int | None = None,
) -> dict[str, Any] | None:
    """Rehydrate the latest committed inference predecessor, read-only."""

    generation_clause = ""
    parameters: list[Any] = [_text(target_identity)]
    if replay_generation is not None:
        generation_clause = " AND replay_generation=?"
        parameters.append(int(replay_generation))
    row = conn.execute(
        f"""
        SELECT posterior_checkpoint_id,posterior_run_id,target_identity,
               joint_posterior_json,joint_posterior_sha256,observed_max_lower_bound_c,
               consumed_event_ids_json,consumed_ancestor_sha256,checkpoint_kind,
               forecast_evidence_snapshot_id,prior_context_id,resolution_contract_id,
               model_artifact_sha256,source_reliability_state_json,
               physical_high_distribution_json,latent_post_state_json,
               committed_at,replay_generation,supersedes_checkpoint_id
        FROM posterior_checkpoints
        WHERE target_identity=? AND state='committed'{generation_clause}
        ORDER BY replay_generation DESC, rowid DESC
        LIMIT 1
        """,
        tuple(parameters),
    ).fetchone()
    if row is None:
        return None
    joint = _json_list(row[3])
    consumed = _json_list(row[6])
    reliability = _json_object(row[13])
    physical = _json_object(row[14])
    latent = _json_object(row[15])
    if (
        joint is None
        or consumed is None
        or reliability is None
        or physical is None
        or latent is None
        or _sha(joint) != _text(row[4])
    ):
        raise ValueError("sequential_bayes_persisted_checkpoint_payload_invalid")
    consumed_ids = [_text(value) for value in consumed if _text(value)]
    return {
        "schema": "posterior_checkpoint_v2",
        "posterior_checkpoint_id": _text(row[0]),
        "posterior_run_id": _text(row[1]),
        "target_identity": _text(row[2]),
        "event_date": _text(event_date)[:10],
        "joint_posterior": joint,
        "joint_posterior_sha256": _text(row[4]),
        "physical_high_distribution": physical,
        "latent_post_state_distribution": latent,
        "observed_max_lower_bound_c": row[5],
        "consumed_event_ids": consumed_ids,
        "source_event_ids": list(consumed_ids),
        "consumed_ancestor_sha256": _text(row[7]),
        "checkpoint_kind": _text(row[8]),
        "forecast_evidence_snapshot_id": _text(row[9]),
        "prior_context_id": _text(row[10]),
        "resolution_contract_id": _text(row[11]),
        "model_artifact_sha256": _text(row[12]),
        "source_reliability_state": reliability,
        "committed_at": _text(row[16]),
        "replay_generation": int(row[17] or 0),
        "supersedes_checkpoint_id": _text(row[18]),
        "state": "committed",
    }


__all__ = ["SCHEMA_VERSION", "load_latest_persisted_checkpoint"]
