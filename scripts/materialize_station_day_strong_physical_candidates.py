#!/usr/bin/env python3
"""Append station-day strong-physical candidate evidence, never training guesses.

The materializer joins only the independent P2 canonical stream and physical
final-X registry.  It deliberately stops before P0/T1/latent replay whenever
one is unavailable, recording a quarantine state instead of inventing a
sample.  A later assembler may consume only candidates whose required frozen
evidence bindings are all present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolution_final_source_runtime import PHYSICAL_ROLE, PHYSICAL_SOURCE_ID  # noqa: E402
from resolution_final_source_shadow_writer import SOURCE_TABLE  # noqa: E402


SCHEMA_VERSION = "station_day_strong_physical_candidate_v1"
DEFAULT_P2_DB = ROOT / "data" / "shadow" / "p2_canonical_amsc_shadow.db"
DEFAULT_FINAL_SOURCE_DB = ROOT / "data" / "shadow" / "resolution_final_labels.db"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "strong_observation" / "physical_candidates"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _safe_root(path: Path) -> Path:
    resolved, canonical = path.expanduser().resolve(), DEFAULT_RESEARCH_ROOT.resolve()
    if resolved != canonical and canonical not in resolved.parents:
        raise ValueError("strong_physical_candidate_root_outside_research_root")
    return resolved


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _load_p2_by_day(path: Path) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
            for raw in conn.execute("""SELECT canonical_observation_event_id,target_identity,station_id,report_type,valid_time,first_actionable_seen_at,state,parsed_observation_json,quality_flags_json FROM canonical_observation_events WHERE station_id='ZUUU' AND state IN ('accepted_primary','accepted_substitute') ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id"""):
                event_date = _text(raw[4])[:10]
                try:
                    parsed, flags = json.loads(_text(raw[7])), json.loads(_text(raw[8]))
                except (TypeError, ValueError):
                    continue
                if event_date and isinstance(parsed, Mapping) and isinstance(flags, list):
                    rows[event_date].append({"canonical_observation_event_id": _text(raw[0]), "target_identity": _text(raw[1]), "station_id": _text(raw[2]), "report_type": _text(raw[3]), "valid_time": _text(raw[4]), "first_actionable_seen_at": _text(raw[5]), "state": _text(raw[6]), "parsed_observation": dict(parsed), "quality_flags": list(flags)})
    except sqlite3.Error as exc:
        return {}, [f"p2_canonical_db_unreadable:{type(exc).__name__}"]
    return dict(rows), []


def _load_physical_x_by_day(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    rows: dict[str, dict[str, Any]] = {}
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
            for (raw_json,) in conn.execute(f"SELECT observation_json FROM {SOURCE_TABLE} WHERE source_id=? AND role=?", (PHYSICAL_SOURCE_ID, PHYSICAL_ROLE)):
                try:
                    observation = json.loads(_text(raw_json))
                except (TypeError, ValueError):
                    continue
                payload = observation.get("payload") if isinstance(observation, Mapping) and isinstance(observation.get("payload"), Mapping) else {}
                day = _text(observation.get("event_date"))[:10] if isinstance(observation, Mapping) else ""
                if day and payload.get("label_finality") == "final":
                    rows[day] = {"event_date": day, "final_physical_high_c": payload.get("final_physical_high_c"), "label_available_at": _text(observation.get("fetched_at")), "label_finality": "final", "strict_asof_training_eligible": payload.get("strict_asof_training_eligible") is True, "raw_sha256": _text(observation.get("raw_sha256")), "source_observation_id": _text(observation.get("source_observation_id")), "payload": dict(payload)}
    except sqlite3.Error as exc:
        return {}, [f"physical_final_x_registry_unreadable:{type(exc).__name__}"]
    return rows, []


def materialize_station_day_strong_physical_candidates(*, p2_db: Path, final_source_db: Path, research_root: Path, allow_research_write: bool = False) -> dict[str, Any]:
    root = _safe_root(research_root)
    p2, p2_issues = _load_p2_by_day(p2_db)
    labels, label_issues = _load_physical_x_by_day(final_source_db)
    outcomes: list[dict[str, Any]] = []
    for day in sorted(labels):
        label, events = labels[day], p2.get(day, [])
        blockers = list(p2_issues) + list(label_issues)
        if not label.get("strict_asof_training_eligible"):
            blockers.append("final_physical_x_not_strict_asof_training_eligible")
        if not events:
            blockers.append("p2_canonical_events_missing_for_station_day")
        state = "candidate_ready_for_future_p0_t1_latent_assembly" if not blockers else "quarantined"
        core = {"schema": SCHEMA_VERSION, "event_date": day, "canonical_events": events, "physical_x_label": label, "state": state, "block_reasons": sorted(set(blockers)), "required_future_bindings": ["full_physical_p0_prior_context", "t1_forecast_evidence_snapshot", "source_reliability_pre_state", "ordered_physical_latent_replay"], "boundary": {"training_scope": "physical_x", "market_finality_not_required": True, "settlement_s_or_bucket_not_required": True, "final_x_is_label_not_feature": True, "writes_posterior": False, "places_orders": False}}
        payload = {**core, "candidate_sha256": _sha(core), "shadow_only": True}
        path = root / f"{day}.{payload['candidate_sha256'][:12]}.json"
        if path.exists():
            outcomes.append({"event_date": day, "state": "skipped_existing_immutable_candidate", "path": str(path), "candidate_state": state})
        elif allow_research_write:
            _atomic_write(path, payload)
            outcomes.append({"event_date": day, "state": "appended_immutable_candidate", "path": str(path), "candidate_state": state})
        else:
            outcomes.append({"event_date": day, "state": "candidate_ready_write_not_authorized", "candidate_state": state})
    return {"schema": SCHEMA_VERSION, "ok": not (p2_issues or label_issues), "state": "materialized" if any(item["state"] == "appended_immutable_candidate" for item in outcomes) else "pending_or_empty", "outcomes": outcomes, "boundary": {"research_only": True, "strict_final_x_required_for_future_training_sample": True, "market_finality_not_required": True, "writes_archive_db": False, "writes_posterior": False, "places_orders": False}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--p2-db", type=Path, default=DEFAULT_P2_DB)
    parser.add_argument("--final-source-db", type=Path, default=DEFAULT_FINAL_SOURCE_DB)
    parser.add_argument("--research-root", type=Path, default=DEFAULT_RESEARCH_ROOT)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args(argv)
    result = materialize_station_day_strong_physical_candidates(p2_db=args.p2_db, final_source_db=args.final_source_db, research_root=args.research_root, allow_research_write=args.allow_research_write)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
