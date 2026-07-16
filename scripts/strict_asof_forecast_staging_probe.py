#!/usr/bin/env python3
"""Prove raw Tier-A capture → proposal → staging → revision in memory only."""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast_revision_canonical_runtime import canonicalize_forecast_revisions  # noqa: E402
from scripts.strict_asof_forecast_capture_readiness import _normalized_forecast  # noqa: E402
from sequential_bayes_public_ssot_shadow_writer import (  # noqa: E402
    initialize_isolated_public_ssot_shadow_schema,
)
from strict_asof_forecast_capture_runtime import (  # noqa: E402
    TIER_A,
    classify_strict_asof_forecast_capture,
)
from strict_asof_forecast_shadow_writer import (  # noqa: E402
    initialize_isolated_forecast_capture_staging_schema,
    persist_tier_a_forecast_source_observation_proposal,
)
from strict_asof_forecast_source_observation_runtime import (  # noqa: E402
    build_tier_a_forecast_source_observation_proposal,
)


SCHEMA_VERSION = "strict_asof_forecast_staging_probe_v1"
DEFAULT_ROOT = ROOT / "data" / "research" / "forecast_source_skill" / "raw"
DEFAULT_SOURCES = ("ecmwf_model_peak", "gfs_model_peak", "icon_model_peak")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _read_tier_a_candidates(root: Path, *, event_date: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for path in sorted(root.expanduser().glob("**/*.json.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                envelope = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(envelope, Mapping):
            continue
        normalized = _normalized_forecast(envelope)
        capture = classify_strict_asof_forecast_capture(
            raw_envelope=envelope,
            normalized_forecast=normalized,
        )
        if _text(capture.get("evidence_tier")) != TIER_A or _text(capture.get("event_date")) != event_date:
            continue
        candidates.append(
            {
                "path": path,
                "capture": capture,
                "normalized_forecast": normalized,
            }
        )
    return candidates


def _select_one_per_source(candidates: Sequence[Mapping[str, Any]], required_sources: Sequence[str]) -> tuple[list[dict[str, Any]], list[str]]:
    selected: list[dict[str, Any]] = []
    missing: list[str] = []
    for source_id in required_sources:
        matches = [
            dict(candidate)
            for candidate in candidates
            if _text(dict(candidate).get("capture", {}).get("source_id")) == _text(source_id)
        ]
        if not matches:
            missing.append(_text(source_id))
            continue
        matches.sort(
            key=lambda item: (
                _text(dict(item.get("capture") or {}).get("first_system_seen_at")),
                str(item.get("path") or ""),
            )
        )
        selected.append(matches[0])
    return selected, missing


def _staged_observations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    columns = [str(row[1] or "") for row in conn.execute("PRAGMA table_info(source_observations)").fetchall()]
    output: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM source_observations ORDER BY source_id,source_observation_id").fetchall():
        value = dict(zip(columns, row))
        try:
            value["payload"] = json.loads(str(value.pop("payload_json") or "{}"))
        except json.JSONDecodeError:
            value["payload"] = {}
        output.append(value)
    return output


def build_strict_asof_forecast_staging_probe(
    *,
    root: Path = DEFAULT_ROOT,
    event_date: str,
    station_id: str = "ZUUU",
    required_source_ids: Sequence[str] = DEFAULT_SOURCES,
) -> dict[str, Any]:
    """Run one complete source/day proof without writing archive or research data."""

    day = _text(event_date)[:10]
    sources = [source for source in (_text(item) for item in required_source_ids) if source]
    candidates = _read_tier_a_candidates(Path(root), event_date=day)
    selected, missing = _select_one_per_source(candidates, sources)
    if not day or not station_id or not sources or missing:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": day,
            "missing_source_ids": missing,
            "guards": {"writes_archive_db": False, "writes_research_files": False, "places_orders": False},
        }
    proposals: list[dict[str, Any]] = []
    for item in selected:
        proposal = build_tier_a_forecast_source_observation_proposal(
            capture=dict(item["capture"]),
            normalized_forecast=dict(item["normalized_forecast"]),
            station_id=station_id,
            raw_ref=f"research://{Path(item['path']).as_posix()}",
        )
        proposals.append(proposal)
    blocked = [proposal for proposal in proposals if proposal.get("ok") is not True]
    if blocked:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": day,
            "proposal_block_reasons": [proposal.get("block_reasons") or [] for proposal in blocked],
            "guards": {"writes_archive_db": False, "writes_research_files": False, "places_orders": False},
        }
    conn = sqlite3.connect(":memory:")
    try:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_isolated_forecast_capture_staging_schema(conn)
        persisted = [persist_tier_a_forecast_source_observation_proposal(conn, proposal) for proposal in proposals]
        rows = _staged_observations(conn)
        latest = max((_parse_time(row.get("fetched_at")) for row in rows), default=None)
        if latest is None:
            raise ValueError("staged_forecast_fetched_at_missing")
        decision_as_of = (latest + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        revisions = canonicalize_forecast_revisions(
            rows,
            target_identity=f"strict-asof-probe-zuuu-{day}",
            event_date=day,
            decision_as_of=decision_as_of,
        )
    except Exception as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": day,
            "error": f"{type(exc).__name__}:{exc}",
            "guards": {"writes_archive_db": False, "writes_research_files": False, "places_orders": False},
        }
    finally:
        conn.close()
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(revisions.get("ok")) and len(rows) == len(sources),
        "state": "proved" if revisions.get("ok") is True and len(rows) == len(sources) else "blocked",
        "event_date": day,
        "selected": [
            {
                "source_id": _text(item["capture"].get("source_id")),
                "raw_path": str(item["path"]),
                "raw_response_sha256": _text(item["capture"].get("raw_response_sha256")),
            }
            for item in selected
        ],
        "persisted": persisted,
        "staged_source_observation_count": len(rows),
        "forecast_revision_result": revisions,
        "decision_as_of": decision_as_of,
        "guards": {
            "writes_archive_db": False,
            "writes_research_files": False,
            "writes_p1_production_tables": False,
            "places_orders": False,
            "staging_backend": "sqlite_memory_isolated_shadow_only",
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--required-source", action="append", default=list(DEFAULT_SOURCES))
    args = parser.parse_args(argv)
    result = build_strict_asof_forecast_staging_probe(
        root=args.root,
        event_date=args.event_date,
        station_id=args.station_id,
        required_source_ids=args.required_source,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
