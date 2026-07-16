"""Build append-only physical X from closed strict AMSC coverage by station day."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping

from amsc_primary_raw_coverage_reader import load_amsc_primary_raw_coverage_observations
from amsc_raw_history_archive import DAILY_COVERAGE_DIRECTORY, DAILY_COVERAGE_SCHEMA, load_amsc_daily_coverage_snapshot
from resolution_final_source_runtime import PHYSICAL_SOURCE_ID, build_amsc_physical_final_source_observation
from resolution_final_source_shadow_writer import SOURCE_TABLE, persist_final_source_observation_shadow


SCHEMA_VERSION = "physical_final_source_shadow_capture_cycle_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _physical_already_persisted(conn: sqlite3.Connection, event_date: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {SOURCE_TABLE} WHERE source_id=? AND event_date=? LIMIT 1",
        (PHYSICAL_SOURCE_ID, _text(event_date)[:10]),
    ).fetchone()
    return row is not None


def _coverage_ref(coverage: Mapping[str, Any]) -> str:
    day = _text(coverage.get("event_date"))[:10]
    sha = _text(coverage.get("coverage_sha256"))
    try:
        year, month, date = day.split("-")
    except ValueError:
        return ""
    return f"{DAILY_COVERAGE_DIRECTORY}/{year}/{month}/{date}/{sha}.json" if sha else ""


def _coverage_event_dates(archive_dir: Path) -> list[str]:
    """Discover station-local AMSC days without any market or finality lookup."""

    root = archive_dir / DAILY_COVERAGE_DIRECTORY
    days: set[str] = set()
    for path in root.glob("*/*/*/*.json") if root.exists() else ():
        try:
            coverage = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(coverage, Mapping):
            continue
        day = _text(coverage.get("event_date"))[:10]
        if (
            _text(coverage.get("schema")) == DAILY_COVERAGE_SCHEMA
            and _text(coverage.get("station_id")).upper() == "ZUUU"
            and len(day) == 10
        ):
            days.add(day)
    return sorted(days, reverse=True)


def run_physical_final_source_shadow_capture_cycle(
    *,
    archive_db_path: str | Path,
    shadow_db_path: str | Path,
    amsc_raw_archive_dir: str | Path,
    authority_contract: Mapping[str, Any],
    finalized_at: str,
    max_targets: int = 4,
    event_dates: list[str] | None = None,
    reader: Callable[..., Mapping[str, Any]] | None = None,
    coverage_loader: Callable[..., Mapping[str, Any] | None] = load_amsc_daily_coverage_snapshot,
) -> dict[str, Any]:
    """Persist X per closed ZUUU AMSC day, independently of the market.

    X is the supervised physical label for weak/strong temperature modelling.
    It must be produced as soon as the station-day schedule closes; a market
    target, market finality, Wunderground S, X/S bridge, and bucket B are all
    separate downstream concerns.
    """

    shadow_path = Path(shadow_db_path).expanduser().resolve()
    archive_path = Path(archive_db_path).expanduser().resolve()
    if not shadow_path.exists() or (reader is not None and not archive_path.exists()):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["physical_final_source_required_db_missing"]}
    try:
        with sqlite3.connect(shadow_path) as conn:
            candidate_days = [
                _text(day)[:10]
                for day in (event_dates if event_dates is not None else _coverage_event_dates(Path(amsc_raw_archive_dir).expanduser().resolve()))
                if _text(day)
            ]
            candidate_days = list(dict.fromkeys(candidate_days))[: max(1, int(max_targets))]
            existing_days = {day: _physical_already_persisted(conn, day) for day in candidate_days}
    except Exception as exc:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": [f"physical_final_source_preflight_failed:{type(exc).__name__}"]}
    outcomes: list[dict[str, Any]] = []
    archive_uri = f"file:{archive_path}?mode=ro"
    for day in candidate_days:
        base = {"event_date": day, "capture_scope": "physical_station_day_not_market_target"}
        if existing_days.get(day):
            outcomes.append({**base, "state": "skipped_existing_physical_source"})
            continue
        try:
            coverage = coverage_loader(amsc_raw_archive_dir, event_date=day, station_id="ZUUU")
            if reader is None:
                # Physical X is owned by immutable AMSC raw membership.  It
                # must not depend on whether an old archive.db fact projection
                # happened to be complete for that station day.
                strict = load_amsc_primary_raw_coverage_observations(
                    amsc_raw_archive_dir,
                    event_date=day,
                    coverage_manifest=coverage or {},
                )
            else:
                # Injectable legacy-shaped reader is retained only as a test
                # seam; the default runtime has no archive.db dependency.
                with sqlite3.connect(archive_uri, uri=True) as archive_conn:
                    strict = reader(
                        archive_conn,
                        event_date=day,
                        archive_dir=amsc_raw_archive_dir,
                        require_strict_asof=False,
                    )
            proposal = build_amsc_physical_final_source_observation(
                event_date=day,
                strict_amsc_reader_result=strict,
                coverage_manifest=coverage or {},
                coverage_ref=_coverage_ref(coverage or {}),
                finalized_at=finalized_at,
                authority_contract=authority_contract,
            )
            if proposal.get("ok") is not True:
                outcomes.append({**base, "state": "pending_physical_coverage", "block_reasons": proposal.get("block_reasons") or []})
                continue
            with sqlite3.connect(shadow_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if _physical_already_persisted(conn, day):
                    conn.rollback()
                    outcomes.append({**base, "state": "skipped_concurrent_physical_source"})
                    continue
                append = persist_final_source_observation_shadow(conn, (proposal.get("source_observation") or {}))
                conn.commit()
            outcomes.append({**base, "state": "appended_physical_source", "append": append})
        except Exception as exc:
            outcomes.append({**base, "state": "blocked", "block_reasons": [f"physical_final_source_capture_failed:{type(exc).__name__}"]})
    blockers = [item for item in outcomes if item.get("state") == "blocked"]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "physical_source_appended" if any(item.get("state") == "appended_physical_source" for item in outcomes) else "pending_or_empty",
        "station_days_considered": len(candidate_days),
        "outcomes": outcomes,
        "boundary": {
            "shadow_only": True,
            "reads_immutable_amsc_raw_lineage_only": reader is None,
            "reads_archive_db": reader is not None,
            "strict_asof_training_eligibility_recorded_separately": True,
            "market_finality_not_read": True,
            "x_to_s_bridge_not_required": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "run_physical_final_source_shadow_capture_cycle"]
