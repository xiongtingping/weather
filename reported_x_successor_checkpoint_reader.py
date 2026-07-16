"""Read-only durable checkpoint reader for the reported-X successor epoch."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from reported_x_successor_checkpoint_runtime import validate_successor_checkpoint_row


SCHEMA_VERSION = "reported_x_successor_checkpoint_reader_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _parse(value: Any, expected: type) -> Any:
    parsed = json.loads(_text(value))
    if not isinstance(parsed, expected):
        raise ValueError("reported_x_checkpoint_json_shape_invalid")
    return parsed


def load_latest_reported_x_checkpoint(
    conn: sqlite3.Connection,
    *,
    target_identity: str,
    event_date: str,
    replay_generation: int | None = None,
) -> dict[str, Any] | None:
    generation_clause = ""
    parameters: list[Any] = [_text(target_identity)]
    if replay_generation is not None:
        generation_clause = " AND replay_generation=?"
        parameters.append(int(replay_generation))
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"""
            SELECT * FROM posterior_checkpoints
            WHERE target_identity=? AND state='committed'{generation_clause}
            ORDER BY replay_generation DESC,rowid DESC LIMIT 1
            """,
            tuple(parameters),
        ).fetchone()
    finally:
        conn.row_factory = previous_factory
    if row is None:
        return None
    raw = dict(row)
    validation = validate_successor_checkpoint_row(raw)
    if validation.get("ok") is not True:
        raise ValueError(
            "reported_x_persisted_checkpoint_invalid:"
            + ",".join(validation.get("issues") or [])
        )
    joint = _parse(raw.get("joint_posterior_json"), list)
    if _sha(joint) != _text(raw.get("joint_posterior_sha256")):
        raise ValueError("reported_x_checkpoint_joint_sha256_mismatch")
    consumed = _parse(raw.get("consumed_event_ids_json"), list)
    reliability = _parse(raw.get("source_reliability_state_json"), dict)
    latent_state = _parse(raw.get("latent_post_state_json"), dict)
    return {
        "schema": _text(raw.get("checkpoint_probability_schema")),
        "posterior_checkpoint_id": _text(raw.get("posterior_checkpoint_id")),
        "posterior_run_id": _text(raw.get("posterior_run_id")),
        "previous_checkpoint_id": _text(raw.get("previous_checkpoint_id")),
        "target_identity": _text(raw.get("target_identity")),
        "event_date": _text(event_date)[:10],
        "joint_posterior": joint,
        "joint_posterior_sha256": _text(raw.get("joint_posterior_sha256")),
        "latent_scheduled_slot_high_distribution": validation[
            "latent_scheduled_slot_high_distribution"
        ],
        "reported_physical_x_distribution": validation[
            "reported_physical_x_distribution"
        ],
        "latent_post_state_distribution": latent_state,
        "observed_max_lower_bound_c": raw.get("latent_observed_lower_bound_c"),
        "latent_observed_lower_bound_c": raw.get("latent_observed_lower_bound_c"),
        "reported_x_lower_bound_c": raw.get("reported_x_lower_bound_c"),
        "reported_x_termination": validation["reported_x_termination"],
        "reported_x_projection_sha256": _text(
            raw.get("reported_x_projection_sha256")
        ),
        "probability_target_type": _text(raw.get("probability_target_type")),
        "consumed_event_ids": [_text(item) for item in consumed if _text(item)],
        "source_event_ids": [_text(item) for item in consumed if _text(item)],
        "consumed_ancestor_sha256": _text(raw.get("consumed_ancestor_sha256")),
        "checkpoint_kind": _text(raw.get("checkpoint_kind")),
        "last_strong_update_id": _text(raw.get("last_strong_update_id")),
        "last_canonical_event_id": _text(raw.get("last_canonical_event_id")),
        "forecast_evidence_snapshot_id": _text(
            raw.get("forecast_evidence_snapshot_id")
        ),
        "prior_context_id": _text(raw.get("prior_context_id")),
        "resolution_contract_id": _text(raw.get("resolution_contract_id")),
        "model_artifact_sha256": _text(raw.get("model_artifact_sha256")),
        "source_reliability_state": reliability,
        "committed_at": _text(raw.get("committed_at")),
        "replay_generation": int(raw.get("replay_generation") or 0),
        "supersedes_checkpoint_id": _text(raw.get("supersedes_checkpoint_id")),
        "state": "committed",
    }


__all__ = ["SCHEMA_VERSION", "load_latest_reported_x_checkpoint"]
