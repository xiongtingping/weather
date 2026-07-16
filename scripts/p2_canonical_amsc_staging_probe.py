#!/usr/bin/env python3
"""Prove raw AMSC primary evidence -> CanonicalObservationEvent in memory.

The archive is opened read-only.  Canonical proposals are persisted twice only
inside SQLite memory to prove append/idempotency behaviour.  The probe cannot
write ``archive.db``, a research artifact, a posterior, a fact, a decision or
an order.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical_observation_runtime import canonicalize_observations  # noqa: E402
from canonical_observation_writer import persist_canonical_observation_proposal  # noqa: E402
from sequential_bayes_schema_contract import apply_forward_schema  # noqa: E402
from strict_amsc_hard_observation_reader import load_strict_amsc_primary_hard_observations  # noqa: E402


SCHEMA_VERSION = "p2_canonical_amsc_staging_probe_v1"
DEFAULT_ARCHIVE = ROOT / "data" / "archive.db"
DEFAULT_RAW_ARCHIVE = ROOT / "data" / "amsc_raw_history"


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


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def build_p2_canonical_amsc_staging_probe(
    *,
    archive_db: Path = DEFAULT_ARCHIVE,
    raw_archive_dir: Path = DEFAULT_RAW_ARCHIVE,
    event_date: str,
    target_identity: str,
) -> dict[str, Any]:
    """Validate actual raw evidence and prove canonical idempotency in memory."""

    day = _text(event_date)[:10]
    target = _text(target_identity)
    if not day or not target:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": day,
            "block_reasons": ["event_date_or_target_identity_missing"],
            "guards": {"writes_archive_db": False, "creates_posterior": False, "places_orders": False},
        }
    try:
        with _read_only_connection(Path(archive_db)) as source_conn:
            loaded = load_strict_amsc_primary_hard_observations(
                source_conn,
                event_date=day,
                archive_dir=raw_archive_dir,
            )
    except (OSError, sqlite3.Error) as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": day,
            "error": type(exc).__name__,
            "block_reasons": ["archive_read_only_open_failed"],
            "guards": {"writes_archive_db": False, "creates_posterior": False, "places_orders": False},
        }
    observations = list(loaded.get("source_observations") or [])
    seen_times = [
        parsed
        for item in observations
        if (parsed := _parse_time(item.get("first_actionable_seen_at"))) is not None
    ]
    last_seen = max(seen_times, default=None)
    if loaded.get("ok") is not True or last_seen is None:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": day,
            "strict_source_read": loaded,
            "block_reasons": ["strict_primary_amsc_source_read_not_available"],
            "guards": {"writes_archive_db": False, "creates_posterior": False, "places_orders": False},
        }
    decision_as_of = (last_seen + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    proposal = canonicalize_observations(
        observations,
        target_identity=target,
        event_date=day,
        decision_as_of=decision_as_of,
    )
    if proposal.get("rejected"):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": day,
            "decision_as_of": decision_as_of,
            "strict_source_read": loaded,
            "canonical_proposal": proposal,
            "block_reasons": ["canonicalization_rejected_strict_primary_source"],
            "guards": {"writes_archive_db": False, "creates_posterior": False, "places_orders": False},
        }
    memory = sqlite3.connect(":memory:")
    try:
        apply_forward_schema(memory)
        first_append = persist_canonical_observation_proposal(memory, proposal)
        existing = list(proposal.get("events") or [])
        retry = canonicalize_observations(
            observations,
            target_identity=target,
            event_date=day,
            decision_as_of=decision_as_of,
            existing_events=existing,
        )
        if retry.get("rejected"):
            raise ValueError("canonical_retry_rejected")
        retry_append = persist_canonical_observation_proposal(memory, retry)
    except Exception as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": day,
            "decision_as_of": decision_as_of,
            "strict_source_read": loaded,
            "canonical_proposal": proposal,
            "error": type(exc).__name__,
            "block_reasons": ["p2_canonical_memory_append_or_retry_failed"],
            "guards": {"writes_archive_db": False, "creates_posterior": False, "places_orders": False},
        }
    finally:
        memory.close()
    idempotent = retry_append.get("inserted_events") == 0 and retry_append.get("inserted_relations") == 0
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(proposal.get("accepted_event_count")) and idempotent,
        "state": "proved" if proposal.get("accepted_event_count") and idempotent else "blocked",
        "event_date": day,
        "target_identity": target,
        "decision_as_of": decision_as_of,
        "strict_source_observation_count": len(observations),
        "strict_source_quarantine": loaded.get("blocked") or [],
        "canonical_accepted_event_count": proposal.get("accepted_event_count"),
        "canonical_event_count": len(proposal.get("events") or []),
        "first_append": first_append,
        "retry_append": retry_append,
        "block_reasons": [] if proposal.get("accepted_event_count") and idempotent else ["canonical_append_idempotency_not_proved"],
        "guards": {
            "archive_opened_read_only": True,
            "writes_archive_db": False,
            "writes_research_files": False,
            "writes_staging_db": False,
            "creates_posterior": False,
            "creates_fact_or_latest": False,
            "creates_decision": False,
            "places_orders": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-db", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--raw-archive-dir", type=Path, default=DEFAULT_RAW_ARCHIVE)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--target-identity", required=True)
    args = parser.parse_args(argv)
    result = build_p2_canonical_amsc_staging_probe(
        archive_db=args.archive_db,
        raw_archive_dir=args.raw_archive_dir,
        event_date=args.event_date,
        target_identity=args.target_identity,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
