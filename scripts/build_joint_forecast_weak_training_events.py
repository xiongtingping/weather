#!/usr/bin/env python3
"""Read-only assembly of strict weak-likelihood training events.

Inputs are deliberately separate: P1 provides only Tier-A forecast facts, the
isolated final-source registry provides only already-closed physical-X targets,
and the caller supplies immutable forecast-only context captures.  The command
never promotes an artifact, writes a runtime pointer, or touches live facts.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from joint_forecast_weak_training_event_runtime import build_joint_weak_training_events  # noqa: E402
from resolution_final_source_runtime import PHYSICAL_ROLE, PHYSICAL_SOURCE_ID  # noqa: E402
from resolution_final_source_shadow_writer import SOURCE_TABLE  # noqa: E402
from strict_asof_forecast_p1_reader import load_strict_forecast_p1_source_observations  # noqa: E402


DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "joint_forecast_weak_likelihood"
DEFAULT_P1_DB = ROOT / "data" / "archive.db"
DEFAULT_FINAL_SOURCE_DB = ROOT / "data" / "shadow" / "resolution_final_labels.db"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _contexts(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [_mapping(row) for row in payload]
    value = _mapping(payload)
    rows = value.get("context_captures")
    return [_mapping(row) for row in rows] if isinstance(rows, list) else []


def _safe_research_path(value: Path) -> Path:
    path = value.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if path != root and root not in path.parents:
        raise ValueError("joint_weak_training_events_output_outside_research_root")
    return path


def _load_final_physical_sources(
    path: Path,
    *,
    event_dates: Sequence[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load only labels belonging to the requested station days.

    A station-day training artifact must not change merely because a different
    day later closes.  The filtered query is therefore part of the training
    identity, not an optimization over a global final-label snapshot.
    """

    days = sorted({str(value or "")[:10] for value in event_dates if str(value or "")[:10]})
    if not days:
        return [], []
    try:
        conn = sqlite3.connect(f"file:{path.expanduser().resolve()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [], [f"final_physical_source_registry_open_failed:{type(exc).__name__}"]
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SOURCE_TABLE,)
        ).fetchone()
        if exists is None:
            return [], ["final_physical_source_registry_table_missing"]
        observations: list[dict[str, Any]] = []
        malformed = 0
        placeholders = ",".join("?" for _ in days)
        for (raw_json,) in conn.execute(
            f"""SELECT observation_json FROM {SOURCE_TABLE}
                WHERE source_id=? AND role=? AND event_date IN ({placeholders})
                ORDER BY event_date,source_observation_id""",
            (PHYSICAL_SOURCE_ID, PHYSICAL_ROLE, *days),
        ):
            try:
                value = json.loads(str(raw_json or ""))
            except (TypeError, ValueError):
                malformed += 1
                continue
            if isinstance(value, Mapping):
                observations.append(dict(value))
            else:
                malformed += 1
        return observations, (["final_physical_source_registry_malformed_json"] if malformed else [])
    finally:
        conn.close()


def build_event_envelope(*, context_captures: Sequence[Mapping[str, Any]], p1_db: Path, final_source_db: Path) -> dict[str, Any]:
    """Build an immutable trainer input; a P1 reader failure is explicit."""

    context_days = sorted({
        str(_mapping(row).get("event_date") or "")[:10]
        for row in context_captures
        if str(_mapping(row).get("event_date") or "")[:10]
    })
    physical_sources, source_blockers = _load_final_physical_sources(
        final_source_db,
        event_dates=context_days,
    )
    observations: list[dict[str, Any]] = []
    p1_by_day: dict[str, Any] = {}
    try:
        conn = sqlite3.connect(f"file:{p1_db.expanduser().resolve()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {
            "schema": "joint_forecast_weak_training_event_envelope_v1",
            "ok": False,
            "state": "blocked",
            "events": [],
            "blockers": sorted(set([*source_blockers, f"p1_forecast_db_open_failed:{type(exc).__name__}"])),
            "boundary": {"read_only": True, "writes_posterior": False, "places_orders": False},
        }
    try:
        for day in context_days:
            reader = load_strict_forecast_p1_source_observations(conn, event_date=str(day))
            p1_by_day[str(day)] = {
                "state": reader.get("state"),
                "source_observation_count": reader.get("source_observation_count"),
                "block_reasons": reader.get("block_reasons") or [],
                "blocked": reader.get("blocked") or [],
            }
            if reader.get("ok") is True:
                observations.extend(_mapping(row) for row in reader.get("source_observations") or [])
    finally:
        conn.close()
    assembly = build_joint_weak_training_events(
        context_captures=context_captures,
        strict_forecast_observations=observations,
        final_physical_source_observations=physical_sources,
    )
    p1_failures = [day for day, value in p1_by_day.items() if value.get("state") != "available"]
    blockers = sorted(set([*source_blockers, *(["strict_forecast_p1_reader_failed_for_context_day"] if p1_failures else [])]))
    return {
        "schema": "joint_forecast_weak_training_event_envelope_v1",
        "ok": assembly.get("ok") is True and not blockers,
        "state": "available" if assembly.get("ok") is True and not blockers else "blocked",
        "events": assembly.get("events") or [],
        "assembly": assembly,
        "p1_reader_by_event_day": p1_by_day,
        "requested_event_dates": context_days,
        "final_physical_source_count": len(physical_sources),
        "blockers": blockers,
        "boundary": {
            "read_only": True,
            "p1_tier_a_reader_only": True,
            "closed_final_physical_x_source_only": True,
            "x_to_s_to_b_bridge_not_required_for_weak_x_training": True,
            "station_day_training_identity_without_market_target_supported": True,
            "training_or_oos_is_not_live_ingest_gate": True,
            "writes_posterior": False,
            "writes_archive_db": False,
            "places_orders": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-captures", required=True, type=Path)
    parser.add_argument("--p1-db", type=Path, default=DEFAULT_P1_DB)
    parser.add_argument("--final-source-db", type=Path, default=DEFAULT_FINAL_SOURCE_DB)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args(argv)
    payload = json.loads(args.context_captures.read_text(encoding="utf-8"))
    result = build_event_envelope(context_captures=_contexts(payload), p1_db=args.p1_db, final_source_db=args.final_source_db)
    if args.output:
        if not args.allow_research_write:
            raise ValueError("joint_weak_training_events_write_requires_explicit_allow_flag")
        output = _safe_research_path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("state") == "available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
