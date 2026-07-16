#!/usr/bin/env python3
"""Archive new ZUUU AMSC SPECI reports and refresh the live strong posterior.

SPECI is never allowed to fill the regular-METAR schedule used to determine
the final physical daily maximum X.  It is nevertheless a same-grade AMSC
real-time observation: after immutable raw archival it enters the auditable
source-fact path and immediately requests one synchronous posterior refresh.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_raw_history_archive import archive_amsc_poll, attach_archive_metadata, raw_text_sha256  # noqa: E402
from production_probability_v6_amsc_archive_ingress import (  # noqa: E402
    apply_v6_amsc_archive_probability_ingress,
)
from production_probability_v6_p0_input_runtime import DEFAULT_RESOLUTION_AUTHORITY  # noqa: E402

DEFAULT_PRODUCTION_ACTIVATION = ROOT / "config" / "production_probability_runtime_activation.json"
from scripts import fetch_zuuu_metar_taf as fetcher  # noqa: E402


SCHEMA_VERSION = "amsc_zuuu_speci_watch_v1"
MAX_SEEN_RAW_SHA256 = 512


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def new_speci_sources(
    source_payloads: Sequence[Mapping[str, Any]],
    *,
    seen_raw_sha256: set[str],
    not_before: datetime | None = None,
) -> list[dict[str, Any]]:
    """Select only unseen SPECI reports; METAR and TAF remain untouched."""

    selected: list[dict[str, Any]] = []
    selected_hashes: set[str] = set()
    for source in source_payloads:
        row = dict(source)
        if str(row.get("report_type") or "").upper() != "SPECI":
            continue
        valid_time = _parse_utc(row.get("valid_time_utc"))
        if not_before is not None and (valid_time is None or valid_time < not_before):
            continue
        raw_sha256 = raw_text_sha256(row.get("raw_text") or row.get("raw_ref"))
        if not raw_sha256 or raw_sha256 in seen_raw_sha256 or raw_sha256 in selected_hashes:
            continue
        row["raw_sha256"] = raw_sha256
        selected.append(row)
        selected_hashes.add(raw_sha256)
    return selected


def build_state(
    previous: Mapping[str, Any],
    *,
    completed_at: str,
    archive_result: Mapping[str, Any],
    new_sources: Sequence[Mapping[str, Any]],
    realtime_ingress: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    known = [str(value) for value in previous.get("seen_raw_sha256") or [] if str(value)]
    new_hashes = [str(source.get("raw_sha256") or "") for source in new_sources]
    seen = list(dict.fromkeys([*known, *new_hashes]))[-MAX_SEEN_RAW_SHA256:]
    archive_objects = _mapping(archive_result.get("raw_objects_by_sha256"))
    reports: list[dict[str, Any]] = []
    for source in new_sources:
        raw_sha256 = str(source.get("raw_sha256") or "")
        archived = _mapping(archive_objects.get(raw_sha256))
        reports.append(
            {
                "report_type": "SPECI",
                "raw_sha256": raw_sha256,
                "valid_time_utc": str(source.get("valid_time_utc") or ""),
                "valid_time_local": str(source.get("valid_time_local") or ""),
                "temperature_c": source.get("temperature_c"),
                "raw_object_ref": str(archived.get("raw_object_ref") or ""),
                "first_seen_at": str(archived.get("first_seen_at") or completed_at),
            }
        )
    ingress = _mapping(realtime_ingress)
    posterior_refreshed = ingress.get("ok") is True
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "new_speci_source_fact_and_posterior_refreshed" if posterior_refreshed else "new_speci_archived_posterior_blocked",
        "updated_at": completed_at,
        "new_speci_count": len(reports),
        "new_speci": reports,
        "seen_raw_sha256": seen,
        "raw_history_archive": dict(archive_result),
        "realtime_strong_ingress": ingress,
        "guards": {
            "writes_archive_db": bool(ingress),
            "writes_facts_sqlite": bool(ingress),
            "writes_posterior": posterior_refreshed,
            "writes_task_queue": False,
            "places_orders": False,
            "historical_backfill": False,
        },
    }


def build_poll_state(
    previous: Mapping[str, Any],
    *,
    completed_at: str,
    state: str,
    realtime_ingress_enabled: bool,
    blockers: Sequence[str] = (),
) -> dict[str, Any]:
    """Build an auditable heartbeat even when a poll yields no observation."""

    seen = [str(value) for value in previous.get("seen_raw_sha256") or [] if str(value)][-MAX_SEEN_RAW_SHA256:]
    ok = state == "no_new_speci"
    return {
        "schema": SCHEMA_VERSION,
        "ok": ok,
        "state": state,
        "updated_at": completed_at,
        "new_speci_count": 0,
        "new_speci": [],
        "seen_raw_sha256": seen,
        "realtime_ingress_enabled": bool(realtime_ingress_enabled),
        "blockers": [str(value) for value in blockers if str(value)],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "writes_task_queue": False,
            "places_orders": False,
            "historical_backfill": False,
        },
    }


def _emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(dict(value), ensure_ascii=False, sort_keys=True), flush=True)


def _fetch_sources(*, count: int, timeout: int) -> tuple[list[dict[str, Any]], str, dict[str, Any], dict[str, Any]]:
    errors: list[str] = []
    for token in fetcher.token_candidates():
        poll_started_at = _utc_now_iso()
        started = time.monotonic()
        try:
            payload, transport = fetcher.fetch_reports_with_transport(token, count, timeout)
        except Exception as exc:
            errors.append(type(exc).__name__)
            continue
        if payload.get("code") != 200:
            errors.append("AMSCResponseNotOK")
            continue
        completed_at = _utc_now_iso()
        sources = fetcher.source_payloads_from_amsc_payload(
            payload,
            fetched_at=completed_at,
            duration_ms=round((time.monotonic() - started) * 1000.0, 3),
        )
        return sources, poll_started_at, dict(payload), {"completed_at": completed_at, "transport": dict(transport)}
    detail = ",".join(sorted(set(errors))) or "session_unavailable"
    raise RuntimeError(f"amsc_speci_watch_fetch_failed:{detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", default=str(ROOT / "data" / "amsc_raw_history"))
    parser.add_argument("--state-path", default=str(ROOT / "data" / "amsc_zuuu_metar_taf" / "speci_watch_latest.json"))
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--max-report-age-sec", type=int, default=900)
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument(
        "--production-probability-ingress",
        action="store_true",
        default=str(os.environ.get("PM_AMSC_ZUUU_SPECI_PRODUCTION_PROBABILITY_INGRESS_ENABLED", "1")).strip() == "1",
        help="append canonical SPECI then synchronously update the unique sequential-Bayes SSOT",
    )
    parser.add_argument("--production-activation", default=str(DEFAULT_PRODUCTION_ACTIVATION))
    parser.add_argument("--resolution-authority", default=str(DEFAULT_RESOLUTION_AUTHORITY))
    args = parser.parse_args(argv)
    if args.count <= 0 or args.timeout <= 0 or args.max_report_age_sec <= 0:
        _emit({"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["arguments_invalid"]})
        return 2

    state_path = Path(args.state_path).expanduser().resolve()
    with _exclusive_lock(state_path.with_suffix(f"{state_path.suffix}.lock")) as acquired:
        if not acquired:
            _emit({"schema": SCHEMA_VERSION, "ok": True, "state": "skipped_previous_watch_running"})
            return 0
        previous = _load_state(state_path)
        seen = {str(value) for value in previous.get("seen_raw_sha256") or [] if str(value)}
        try:
            sources, poll_started_at, payload, fetch = _fetch_sources(count=args.count, timeout=args.timeout)
        except RuntimeError as exc:
            failed = build_poll_state(
                previous,
                completed_at=_utc_now_iso(),
                state="source_fetch_failed",
                realtime_ingress_enabled=bool(args.production_probability_ingress),
                blockers=[str(exc)],
            )
            _atomic_json(state_path, failed)
            _emit(failed)
            return 1
        completed_at = str(fetch["completed_at"])
        completed_dt = _parse_utc(completed_at)
        assert completed_dt is not None
        new_sources = new_speci_sources(
            sources,
            seen_raw_sha256=seen,
            not_before=completed_dt - timedelta(seconds=args.max_report_age_sec),
        )
        if not new_sources:
            no_new = build_poll_state(
                previous,
                completed_at=completed_at,
                state="no_new_speci",
                realtime_ingress_enabled=bool(args.production_probability_ingress),
            )
            _atomic_json(state_path, no_new)
            _emit(no_new)
            return 0

        transport = _mapping(fetch.get("transport"))
        archive_result = archive_amsc_poll(
            archive_dir=args.archive_dir,
            source_payloads=new_sources,
            poll_started_at=poll_started_at,
            response_completed_at=completed_at,
            response_started_at=str(transport.get("response_started_at") or ""),
            response_headers_observed=transport.get("response_headers_observed") is True,
            response_status=payload.get("code"),
            request_metadata={"cccc": "ZUUU", "count": args.count, "tt": "SA,SP,FC,FT,"},
            fetcher_version=f"{fetcher.PARSER_VERSION}:speci_watch_v1",
        )
        archived_sources = attach_archive_metadata(new_sources, archive_result)
        source_bundle = fetcher.build_amsc_observation_bundle(
            payload,
            fetched_at=completed_at,
            source_payloads=archived_sources,
            raw_history_archive=archive_result,
        )
        source_observation_payload = {
            "source_observations": list(source_bundle.get("source_observations") or []),
            "source_observation_ids": list(source_bundle.get("source_observation_ids") or []),
            "source_acquisition_trace": dict(source_bundle.get("source_acquisition_trace") or {}),
        }
        realtime_ingress: Mapping[str, Any] = {
            "schema": "amsc_speci_realtime_strong_ingress_v1",
            "ok": False,
            "state": "disabled",
            "places_orders": False,
        }
        if args.production_probability_ingress:
            try:
                realtime_ingress = apply_v6_amsc_archive_probability_ingress(
                    db_path=args.db,
                    raw_archive_dir=args.archive_dir,
                    event_date=str(new_sources[0].get("event_date") or "")[:10],
                    decision_as_of=completed_at,
                    activation_path=args.production_activation,
                    resolution_authority_path=args.resolution_authority,
                    source_observation_payload=source_observation_payload,
                    source_observation_created_at=completed_at,
                    writer_queue_wait_sec=0.75,
                    db_timeout_sec=0.75,
                    db_busy_timeout_ms=500,
                )
            except Exception as exc:  # Canonical/raw evidence remains durable on probability failure.
                realtime_ingress = {
                    "schema": "production_probability_v6_amsc_archive_ingress_v1",
                    "ok": False,
                    "state": "exception_after_raw_archive",
                    "blockers": [f"{type(exc).__name__}:{exc}"],
                    "places_orders": False,
                }
        state = build_state(
            previous,
            completed_at=completed_at,
            archive_result=archive_result,
            new_sources=new_sources,
            realtime_ingress=realtime_ingress,
        )
        _atomic_json(state_path, state)
        _emit(state)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
