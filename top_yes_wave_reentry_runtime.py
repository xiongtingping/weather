"""Auditable same-posterior re-entry suppression for completed top-YES waves."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "top_yes_wave_reentry_state_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def build_top_yes_wave_reentry_state(
    *,
    posterior_run_id: str,
    completed_cycles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    run = _text(posterior_run_id)
    rows: list[dict[str, str]] = []
    issues: list[str] = []
    if not run:
        issues.append("top_yes_wave_reentry_posterior_run_missing")
    for raw in completed_cycles:
        cycle_id = _text(raw.get("market_alpha_cycle_id"))
        cycle_run = _text(raw.get("posterior_run_id"))
        bucket = _text(raw.get("bucket"))
        state = _text(raw.get("state"))
        if not cycle_id or cycle_run != run or not bucket or state != "completed":
            issues.append("top_yes_wave_reentry_completed_cycle_invalid")
            continue
        rows.append({"market_alpha_cycle_id": cycle_id, "posterior_run_id": cycle_run, "bucket": bucket, "state": state})
    if issues:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(issues)), "read_only": True}
    rows.sort(key=lambda row: (row["bucket"], row["market_alpha_cycle_id"]))
    identity = {"posterior_run_id": run, "completed_cycles": rows}
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "top_yes_wave_reentry_state_id": "wavereentry_" + hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()[:24],
        **identity,
        "suppressed_buckets": sorted({row["bucket"] for row in rows}),
        "unlock_condition": "new_posterior_run_id",
        "bucket_selection_remains_dynamic": True,
        "read_only": True,
    }


__all__ = ["SCHEMA_VERSION", "build_top_yes_wave_reentry_state"]
