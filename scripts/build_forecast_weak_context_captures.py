#!/usr/bin/env python3
"""Build forecast-only context captures from explicit cutoff requests and P1."""

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

from forecast_weak_context_capture_runtime import build_forecast_weak_context_capture  # noqa: E402
from strict_asof_forecast_p1_reader import load_strict_forecast_p1_source_observations  # noqa: E402


DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "joint_forecast_weak_likelihood"
DEFAULT_P1_DB = ROOT / "data" / "archive.db"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _cutoffs(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [_mapping(row) for row in value]
    rows = _mapping(value).get("cutoffs")
    return [_mapping(row) for row in rows] if isinstance(rows, list) else []


def _safe_research_path(value: Path) -> Path:
    path = value.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if path != root and root not in path.parents:
        raise ValueError("forecast_weak_context_capture_output_outside_research_root")
    return path


def build_context_capture_envelope(*, cutoffs: Sequence[Mapping[str, Any]], p1_db: Path) -> dict[str, Any]:
    """Read P1 once and produce one immutable context capture per cutoff."""

    captures: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{p1_db.expanduser().resolve()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"schema": "forecast_weak_context_capture_envelope_v1", "ok": False, "state": "blocked", "captures": [], "quarantined": [], "blockers": [f"p1_forecast_db_open_failed:{type(exc).__name__}"], "boundary": {"read_only": True, "writes_posterior": False, "places_orders": False}}
    try:
        reader_by_day: dict[str, dict[str, Any]] = {}
        for cutoff in cutoffs:
            day = str(_mapping(cutoff).get("event_date") or "")[:10]
            if day and day not in reader_by_day:
                reader_by_day[day] = load_strict_forecast_p1_source_observations(conn, event_date=day)
        for raw in cutoffs:
            cutoff = _mapping(raw)
            day = str(cutoff.get("event_date") or "")[:10]
            reader = reader_by_day.get(day, {})
            if reader.get("ok") is not True:
                quarantined.append({"cutoff": cutoff, "block_reasons": ["strict_forecast_p1_reader_not_available", *list(reader.get("block_reasons") or [])]})
                continue
            capture = build_forecast_weak_context_capture(
                target_identity=str(cutoff.get("target_identity") or ""),
                event_date=day,
                decision_as_of=str(cutoff.get("decision_as_of") or ""),
                strict_forecast_observations=reader.get("source_observations") or [],
                station_id=str(cutoff.get("station_id") or "ZUUU"),
                target_timezone=str(cutoff.get("target_timezone") or "Asia/Shanghai"),
            )
            if capture.get("ok") is True:
                captures.append(capture)
            else:
                quarantined.append({"cutoff": cutoff, "block_reasons": capture.get("block_reasons") or []})
    finally:
        conn.close()
    return {
        "schema": "forecast_weak_context_capture_envelope_v1",
        "ok": bool(captures) and not quarantined,
        "state": "available" if captures and not quarantined else "blocked",
        "context_captures": captures,
        "quarantined": quarantined,
        "boundary": {"read_only": True, "p1_tier_a_only": True, "station_day_training_identity_without_market_target_supported": True, "writes_posterior": False, "writes_archive_db": False, "places_orders": False, "live_amsc_ingest_blocked": False},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cutoffs", required=True, type=Path)
    parser.add_argument("--p1-db", type=Path, default=DEFAULT_P1_DB)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args(argv)
    payload = json.loads(args.cutoffs.read_text(encoding="utf-8"))
    result = build_context_capture_envelope(cutoffs=_cutoffs(payload), p1_db=args.p1_db)
    if args.output:
        if not args.allow_research_write:
            raise ValueError("forecast_weak_context_capture_write_requires_explicit_allow_flag")
        output = _safe_research_path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("state") == "available" else 1


if __name__ == "__main__":
    raise SystemExit(main())
