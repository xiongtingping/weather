#!/usr/bin/env python3
"""Materialize immutable station-day weak/X candidate events in research storage.

Closed AMSC physical-X sources select the event days.  P1 forecasts are read
strictly as-of fixed local cutoffs; missing Tier-A forecast evidence is
quarantined in the emitted research artifact, not repaired or allowed to block
live observation ingestion.  No market target, market finality, S, or bridge
is read to create these physical-X candidate events.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolution_final_source_runtime import PHYSICAL_ROLE, PHYSICAL_SOURCE_ID  # noqa: E402
from resolution_final_source_shadow_writer import SOURCE_TABLE  # noqa: E402
from scripts.build_forecast_weak_context_captures import build_context_capture_envelope  # noqa: E402
from scripts.build_joint_forecast_weak_training_events import build_event_envelope  # noqa: E402
from station_day_weak_physical_training_runtime import DEFAULT_LOCAL_CUTOFFS, build_station_day_weak_context_requests  # noqa: E402


SCHEMA_VERSION = "station_day_weak_physical_training_materialization_v3"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "joint_forecast_weak_likelihood" / "station_day_candidates"
DEFAULT_FINAL_SOURCE_DB = ROOT / "data" / "shadow" / "resolution_final_labels.db"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _safe_root(value: Path) -> Path:
    root = value.expanduser().resolve()
    canonical = DEFAULT_RESEARCH_ROOT.resolve()
    if root != canonical and canonical not in root.parents:
        raise ValueError("station_day_weak_materialization_root_outside_research_root")
    return root


def _closed_physical_days(path: Path, requested: Sequence[str]) -> list[str]:
    requested_days = sorted({_text(value)[:10] for value in requested if _text(value)})
    try:
        with sqlite3.connect(f"file:{path.expanduser().resolve()}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                f"""SELECT event_date FROM {SOURCE_TABLE}
                    WHERE source_id=? AND role=?
                    ORDER BY event_date""",
                (PHYSICAL_SOURCE_ID, PHYSICAL_ROLE),
            ).fetchall()
    except sqlite3.Error:
        return []
    available = sorted({_text(row[0])[:10] for row in rows if _text(row[0])})
    return [day for day in available if not requested_days or day in requested_days]


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _read_candidate(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _candidate_revision(candidate: Mapping[str, Any]) -> int:
    """Return explicit revision, retaining deterministic precedence for v1/v2."""

    try:
        revision = int(candidate.get("candidate_revision") or 0)
    except (TypeError, ValueError):
        revision = 0
    if revision > 0:
        return revision
    return 2 if isinstance(candidate.get("supersedes"), Mapping) else 1


def _latest_candidate(root: Path, event_date: str) -> tuple[int, Path, dict[str, Any]] | None:
    candidates: list[tuple[int, Path, dict[str, Any]]] = []
    for path in sorted(root.glob(f"{event_date}*.json")):
        candidate = _read_candidate(path)
        if _text(candidate.get("event_date"))[:10] != event_date:
            continue
        candidates.append((_candidate_revision(candidate), path, candidate))
    return max(candidates, key=lambda item: (item[0], str(item[1]))) if candidates else None


def _input_lineage_core(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the station-day inputs, never append bookkeeping."""

    return {
        key: candidate.get(key)
        for key in (
            "schema",
            "event_date",
            "requests",
            "context_capture_envelope",
            "weak_training_event_envelope",
        )
    }


def _input_lineage_sha256(candidate: Mapping[str, Any]) -> str:
    recorded = _text(candidate.get("input_lineage_sha256"))
    return recorded or _sha(_input_lineage_core(candidate))


def _materialization_core(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in candidate.items()
        if key not in {"materialization_sha256", "read_only_inputs", "shadow_only", "boundary"}
    }


def materialize_station_day_weak_physical_training(
    *,
    p1_db: Path,
    final_source_db: Path,
    research_root: Path,
    event_dates: Sequence[str] = (),
    local_cutoffs: Sequence[str] = DEFAULT_LOCAL_CUTOFFS,
    allow_research_write: bool = False,
) -> dict[str, Any]:
    """Append one immutable candidate artifact per closed physical station day."""

    root = _safe_root(research_root)
    days = _closed_physical_days(final_source_db, event_dates)
    requests = build_station_day_weak_context_requests(event_dates=days, local_cutoffs=local_cutoffs)
    by_day: dict[str, list[dict[str, Any]]] = {day: [] for day in days}
    for request in list(requests.get("requests") or []):
        day = _text(request.get("event_date"))[:10]
        if day in by_day:
            by_day[day].append(dict(request))
    outcomes: list[dict[str, Any]] = []
    for day in days:
        contexts = build_context_capture_envelope(cutoffs=by_day.get(day, []), p1_db=p1_db)
        events = build_event_envelope(
            context_captures=list(contexts.get("context_captures") or []),
            p1_db=p1_db,
            final_source_db=final_source_db,
        )
        input_core = {
            "schema": SCHEMA_VERSION,
            "event_date": day,
            "requests": [dict(row) for row in by_day.get(day, [])],
            "context_capture_envelope": contexts,
            "weak_training_event_envelope": events,
        }
        payload: dict[str, Any] = {
            **input_core,
            "input_lineage_sha256": _sha(input_core),
            "candidate_revision": 1,
            "read_only_inputs": True,
            "shadow_only": True,
            "boundary": {
                "station_day_identity_only": True,
                "market_target_not_required": True,
                "market_finality_not_read": True,
                "x_to_s_to_b_bridge_not_required": True,
                "training_or_oos_is_not_live_ingest_gate": True,
                "writes_archive_db": False,
                "writes_posterior": False,
                "places_orders": False,
            },
        }
        payload["materialization_sha256"] = _sha(_materialization_core(payload))
        primary_destination = root / f"{day}.json"
        destination = primary_destination
        latest = _latest_candidate(root, day)
        if latest is not None:
            latest_revision, latest_path, existing = latest
            if _input_lineage_sha256(existing) == _text(payload.get("input_lineage_sha256")):
                outcomes.append({"event_date": day, "state": "skipped_existing_immutable_candidate", "path": str(latest_path)})
                continue
            # Never overwrite a historical candidate.  Any changed, scoped
            # input receives an append-only successor; loaders select the
            # greatest revision rather than guessing by filename alone.
            reason = (
                "v1_all_or_nothing_source_quarantine_contract_retired"
                if _text(existing.get("schema")) == "station_day_weak_physical_training_materialization_v1"
                else "event_scoped_input_lineage_changed_after_immutable_materialization"
            )
            payload["candidate_revision"] = latest_revision + 1
            payload["supersedes"] = {
                "path": str(latest_path),
                "materialization_sha256": _text(existing.get("materialization_sha256")),
                "candidate_revision": latest_revision,
                "reason": reason,
            }
            payload["materialization_sha256"] = _sha(_materialization_core(payload))
            destination = root / f"{day}.v{payload['candidate_revision']}-{payload['materialization_sha256'][:12]}.json"
            if destination.exists():
                outcomes.append({"event_date": day, "state": "skipped_existing_correction_candidate", "path": str(destination)})
                continue
        if not allow_research_write:
            outcomes.append({"event_date": day, "state": "candidate_ready_write_not_authorized", "candidate": payload})
            continue
        _atomic_write(destination, payload)
        outcomes.append({
            "event_date": day,
            "state": "appended_immutable_candidate",
            "path": str(destination),
            "materialization_sha256": payload["materialization_sha256"],
            "candidate_revision": payload["candidate_revision"],
            "supersedes_immutable_candidate": bool(payload.get("supersedes")),
        })
    return {
        "schema": SCHEMA_VERSION,
        # No closed station day is a normal steady-state condition between
        # daily boundaries, never a live-path error or retry storm trigger.
        "ok": True,
        "state": "materialized" if any(item.get("state") == "appended_immutable_candidate" for item in outcomes) else "pending_or_empty",
        "closed_station_day_count": len(days),
        "outcomes": outcomes,
        "boundary": {
            "station_day_identity_only": True,
            "market_target_not_required": True,
            "market_finality_not_read": True,
            "x_to_s_to_b_bridge_not_required": True,
            "training_or_oos_is_not_live_ingest_gate": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--p1-db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--final-source-db", type=Path, default=DEFAULT_FINAL_SOURCE_DB)
    parser.add_argument("--research-root", type=Path, default=DEFAULT_RESEARCH_ROOT)
    parser.add_argument("--event-date", action="append", default=[])
    parser.add_argument("--cutoff-local", default=",".join(DEFAULT_LOCAL_CUTOFFS))
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args(argv)
    cutoffs = [item.strip() for item in str(args.cutoff_local).split(",") if item.strip()]
    result = materialize_station_day_weak_physical_training(
        p1_db=args.p1_db,
        final_source_db=args.final_source_db,
        research_root=args.research_root,
        event_dates=list(args.event_date),
        local_cutoffs=cutoffs,
        allow_research_write=args.allow_research_write,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
